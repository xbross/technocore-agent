"""Contre-signature automatique d'un roster externe, sous conditions strictes (pre-accord de Xav,
2026-09-14) : le message roster.v1 doit etre signe par UN lead precis, pour UN jeu precis, contenir
notre DID, avoir 4 a 8 membres et viser la room du jeu. Si le lead revise la liste, on retire notre
consentement puis on signe la nouvelle liste. Chaque ecriture attend le recu signe de l'arbitre.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable

from ..client import ApiError, Duplicate, NetworkError, RateLimited
from ..identity import Identity
from .lexicon import ED25519_DID
from .team import roster_message, withdraw_message
from .watch import classify

log = logging.getLogger("technocore.sonnet.countersign")


class CounterSigner:
    def __init__(self, client, ident: Identity, referee_did: str, contest_id: str, game_id: str | None,
                 lead_did: str | None = None, discovery_room: str = "mb-sonnet-2-discovery", dry_run: bool = True,
                 receipt_wait_s: float = 120, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, archive=None, lead_dids: set[str] | None = None,
                 release_game: str | None = None, hold_path=None, policy: Callable | None = None):
        """game_id=None : n'importe quel jeu (la room doit correspondre au game_id du roster).
        lead_dids : ensemble des leads acceptes (lead_did reste accepte pour compatibilite).
        release_game : notre propre equipe, dont on libere le consentement avant de signer ailleurs.
        hold_path : fichier ecrit apres une signature externe, lu par le gestionnaire pour se mettre en pause."""
        self.client, self.ident, self.referee_did = client, ident, referee_did
        self.contest_id, self.game_id = contest_id, game_id
        self.leads = set(lead_dids or set()) | ({lead_did} if lead_did else set())
        self.lead_did = lead_did or (sorted(self.leads)[0] if self.leads else "")
        self.room, self.dry_run, self.receipt_wait_s = discovery_room, dry_run, receipt_wait_s
        self.now, self.sleep, self.archive = now, sleep, archive
        self.release_game, self.hold_path = release_game, hold_path
        self.policy = policy  # fonction (msg, data) -> bool, evaluee si l'expediteur n'est pas dans la liste blanche
        self.cursor = 0
        self.signed_game: str | None = None
        self.signed_members: list[str] | None = None
        self.attempted: set[tuple] = set()  # listes deja tentees : jamais de nouvelle tentative
        self.ready = False
        self._last_nonce = 0
        self._counter = 0

    def _rid(self, kind: str, game: str | None = None) -> str:
        self._counter += 1
        return f"xav-{game or self.game_id}-{kind}-{int(self.now() * 1000)}-{self._counter}"

    def _release(self, game: str, what: str) -> bool:
        """Retire notre consentement sur `game` ; True si accepte (ou deja absent)."""
        rid = self._rid("wd", game)
        res = self._post(withdraw_message(self.contest_id, game, rid))
        rec = self._await_receipt(rid, res.seq or self.cursor)
        if rec and (rec.get("status") == "accepted" or "missing" in str(rec.get("reason", ""))):
            return True
        log.warning("%s: %s non confirme (%s)", game, what, rec)
        return False

    def _nonce(self) -> int:
        n = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = n
        return n

    def acceptable(self, msg) -> dict | None:
        """Le roster du lead qui nous nomme, ou None."""
        if not msg.signed or msg.sender == self.referee_did:
            return None
        try:
            data = json.loads(msg.text)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("type") != "sonnet.roster.v1":
            return None
        if msg.sender not in self.leads and not (self.policy is not None and self.policy(msg, data)):
            return None
        game = data.get("game_id")
        if not isinstance(game, str) or not re.match(r"^[a-z0-9][a-z0-9_-]{0,15}$", game):
            return None
        if self.game_id is not None and game != self.game_id:
            return None
        members = data.get("members")
        if not isinstance(members, list) or not 4 <= len(members) <= 8 or self.ident.did not in members:
            return None
        if not all(isinstance(m, str) and ED25519_DID.fullmatch(m) for m in members) or len(set(members)) != len(members):
            return None
        if data.get("poem_room") != f"d-sonnet-2-team-{game}":
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
        game = str(target["game_id"])
        if members == self.signed_members and game == self.signed_game:
            return {"action": "already-signed"}
        if (game, tuple(members)) in self.attempted:
            return {"action": "wait"}
        self.attempted.add((game, tuple(members)))
        log.info("%s: roster d'un lead autorise nous nomme (%d membres, gen %s)", game, len(members),
                 target.get("room_generation"))
        if self.dry_run:
            return {"action": "planned", "game": game, "members": members}
        if self.signed_members is not None and self.signed_game:  # liste revisee ou autre equipe : liberer d'abord
            if not self._release(self.signed_game, "retrait"):
                return {"action": "withdraw-failed"}
            self.signed_members, self.signed_game = None, None
        elif self.release_game and self.release_game != game:  # notre propre equipe (gestionnaire)
            if not self._release(self.release_game, "liberation de notre equipe"):
                return {"action": "withdraw-failed"}
        rid = self._rid("roster", game)
        text = roster_message(self.contest_id, game, str(target["poem_room"]), int(target["room_generation"]),
                              members, rid)
        res = self._post(text)
        rec = self._await_receipt(rid, res.seq or self.cursor)
        if not rec or rec.get("status") != "accepted":
            log.warning("%s: signature non confirmee (%s)", game, rec)
            return {"action": "sign-failed", "receipt": rec}
        self.signed_members, self.signed_game = members, game
        if self.hold_path:
            from pathlib import Path
            Path(self.hold_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.hold_path).write_text(json.dumps({"game": game, "members": members, "at": self.now()}), "utf-8")
        log.info("%s: notre contre-signature est acceptee (roster_ready=%s)", game, rec.get("roster_ready"))
        return {"action": "signed", "game": game, "members": members, "roster_ready": rec.get("roster_ready")}

    def run(self, stop=None, poll_seconds: float = 5, long_poll: int = 10) -> None:
        log.info("%s: contre-signature automatique armee pour %d lead(s) (%s)", self.game_id or "tout jeu",
                 len(self.leads), "DRY-RUN" if self.dry_run else "LIVE")
        while not (stop is not None and stop.is_set()):
            try:
                out = self.step(wait=long_poll)
            except (NetworkError, ApiError, RateLimited) as e:
                log.warning("%s: %s", self.game_id, e)
                out = {"action": "error"}
            if out["action"] not in ("wait", "already-signed"):
                log.info("%s: %s", self.game_id, json.dumps(out)[:300])
            if self.ready:
                log.info("%s: roster pret ; la contre-signature automatique s'arrete", self.signed_game or self.game_id)
                return
            if out["action"] in ("error", "withdraw-failed", "sign-failed"):
                self.sleep(60)
            elif stop is not None:
                stop.wait(poll_seconds)
            else:
                self.sleep(poll_seconds)
