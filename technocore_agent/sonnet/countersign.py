"""Contre-signature automatique d'un roster externe, sous conditions strictes (pre-accord de Xav,
2026-09-14) : le message roster.v1 doit etre signe par UN lead precis, pour UN jeu precis, contenir
notre DID, avoir 4 a 8 membres et viser la room du jeu. Si le lead revise la liste, on retire notre
consentement puis on signe la nouvelle liste. Chaque ecriture attend le recu signe de l'arbitre.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

from ..client import ApiError, Duplicate, NetworkError, RateLimited
from ..identity import Identity
from .lexicon import ED25519_DID
from .team import roster_message, withdraw_message
from .watch import classify

log = logging.getLogger("technocore.sonnet.countersign")


class CounterSigner:
    def __init__(self, client, ident: Identity, referee_did: str, contest_id: str, game_id: str, lead_did: str,
                 discovery_room: str, dry_run: bool = True, receipt_wait_s: float = 120,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep, archive=None):
        self.client, self.ident, self.referee_did = client, ident, referee_did
        self.contest_id, self.game_id, self.lead_did = contest_id, game_id, lead_did
        self.room, self.dry_run, self.receipt_wait_s = discovery_room, dry_run, receipt_wait_s
        self.now, self.sleep, self.archive = now, sleep, archive
        self.cursor = 0
        self.signed_members: list[str] | None = None
        self.ready = False
        self._last_nonce = 0
        self._counter = 0

    def _rid(self, kind: str) -> str:
        self._counter += 1
        return f"xav-{self.game_id}-{kind}-{int(self.now() * 1000)}-{self._counter}"

    def _nonce(self) -> int:
        n = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = n
        return n

    def acceptable(self, msg) -> dict | None:
        """Le roster du lead qui nous nomme, ou None."""
        if msg.sender != self.lead_did or not msg.signed:
            return None
        try:
            data = json.loads(msg.text)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("type") != "sonnet.roster.v1" or data.get("game_id") != self.game_id:
            return None
        members = data.get("members")
        if not isinstance(members, list) or not 4 <= len(members) <= 8 or self.ident.did not in members:
            return None
        if not all(isinstance(m, str) and ED25519_DID.fullmatch(m) for m in members) or len(set(members)) != len(members):
            return None
        if data.get("poem_room") != f"d-sonnet-2-team-{self.game_id}":
            return None
        return data

    def _await_receipt(self, request_id: str, since: int) -> dict | None:
        deadline = self.now() + self.receipt_wait_s
        cursor = since
        while True:
            try:
                page = self.client.read(self.room, since=cursor)
                for m in page.messages:
                    if m.sender == self.referee_did and classify(m, self.room, self.referee_did) == "receipt":
                        d = json.loads(m.text)
                        if d.get("type") == "sonnet.receipts.v1":
                            for r in d.get("receipts", []):
                                if r.get("request_id") == request_id and r.get("sender_did") == self.ident.did:
                                    return {**r, "status": d.get("status"), "reason": d.get("reason", "")}
                        elif d.get("request_id") == request_id and d.get("sender_did") == self.ident.did:
                            if d.get("roster_ready") is True:
                                self.ready = True
                            return d
                if page.last_seq is not None:
                    cursor = max(cursor, int(page.last_seq))
            except RateLimited as e:
                self.sleep(min(e.retry_after, 30))
            except (NetworkError, ApiError, Duplicate) as e:
                log.warning("lecture impossible (%s)", e)
            if self.now() >= deadline:
                return None
            self.sleep(3)

    def _post(self, text: str):
        res = self.client.say_signed(self.ident, self.room, text, self._nonce())
        if self.archive:
            self.archive.append(self.room, {"kind": "countersign-post", "text": text, "seq": res.seq})
        return res

    def step(self, wait: int | None = None) -> dict:
        page = self.client.read(self.room, since=self.cursor, wait=wait)
        target = None
        for m in sorted(page.messages, key=lambda m: m.seq):
            self.cursor = max(self.cursor, m.seq)
            data = self.acceptable(m)
            if data:
                target = data
            elif m.sender == self.referee_did and classify(m, self.room, self.referee_did) == "receipt":
                d = json.loads(m.text)
                if d.get("sender_did") == self.ident.did and d.get("roster_ready") is True:
                    self.ready = True
        if target is None:
            return {"action": "wait"}
        members = list(target["members"])
        if members == self.signed_members:
            return {"action": "already-signed"}
        log.info("%s: roster du lead ...%s nous nomme (%d membres, gen %s)", self.game_id, self.lead_did[-8:],
                 len(members), target.get("room_generation"))
        if self.dry_run:
            return {"action": "planned", "members": members}
        if self.signed_members is not None:  # liste revisee : liberer d'abord notre consentement
            rid = self._rid("wd")
            res = self._post(withdraw_message(self.contest_id, self.game_id, rid))
            rec = self._await_receipt(rid, res.seq or self.cursor)
            if not rec or rec.get("status") != "accepted":
                log.warning("%s: retrait non confirme (%s)", self.game_id, rec)
                return {"action": "withdraw-failed", "receipt": rec}
            self.signed_members = None
        rid = self._rid("roster")
        text = roster_message(self.contest_id, self.game_id, str(target["poem_room"]), int(target["room_generation"]),
                              members, rid)
        res = self._post(text)
        rec = self._await_receipt(rid, res.seq or self.cursor)
        if not rec or rec.get("status") != "accepted":
            log.warning("%s: signature non confirmee (%s)", self.game_id, rec)
            return {"action": "sign-failed", "receipt": rec}
        self.signed_members = members
        log.info("%s: notre contre-signature est acceptee (roster_ready=%s)", self.game_id, rec.get("roster_ready"))
        return {"action": "signed", "members": members, "roster_ready": rec.get("roster_ready")}

    def run(self, stop=None, poll_seconds: float = 5, long_poll: int = 10) -> None:
        log.info("%s: contre-signature automatique armee pour le lead ...%s (%s)", self.game_id, self.lead_did[-8:],
                 "DRY-RUN" if self.dry_run else "LIVE")
        while not (stop is not None and stop.is_set()):
            try:
                out = self.step(wait=long_poll)
            except (NetworkError, ApiError, RateLimited) as e:
                log.warning("%s: %s", self.game_id, e)
                out = {"action": "error"}
            if out["action"] not in ("wait", "already-signed"):
                log.info("%s: %s", self.game_id, json.dumps(out)[:300])
            if self.ready:
                log.info("%s: roster pret ; la contre-signature automatique s'arrete", self.game_id)
                return
            if out["action"] in ("error", "withdraw-failed", "sign-failed"):
                self.sleep(60)
            elif stop is not None:
                stop.wait(poll_seconds)
            else:
                self.sleep(poll_seconds)
