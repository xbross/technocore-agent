"""Gestionnaire de roster (cote lead) : remplace les sieges qui ne signent pas.

Regle (validee par Xav le 2026-09-13) : un siege en attente depuis plus que `patience_s`, ou dont
la signature sur notre liste a ete rejetee pour un consentement anterieur, est remplace par le
meilleur writer libre : actif dans discovery depuis moins de `active_window_s`, jamais signataire
d'un roster dans l'export (ou dont le dernier evenement accepte est un retrait), pas lead d'une
autre equipe, et qui garde les 26 lettres couvertes. Chaque remplacement = retrait de notre
consentement, re-signature de la nouvelle liste, invitation nominative. Tout est archive.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..client import ApiError, Duplicate, NetworkError, RateLimited
from ..identity import Identity
from ..safety import check_reply
from .lexicon import ED25519_DID, roster_coverage
from .roster import roster_status
from .team import roster_message, withdraw_message
from .watch import classify

log = logging.getLogger("technocore.sonnet.manager")


def parse_ts(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


@dataclass
class Rules:
    patience_s: float = 45 * 60
    active_window_s: float = 90 * 60
    max_replacements: int = 12
    receipt_wait_s: float = 120


@dataclass
class Profile:
    did: str
    signed_any: bool = False
    free_by_withdraw: bool = False
    is_lead: bool = False
    last_activity: float | None = None
    applied_to: set = field(default_factory=set)
    rejected_on: dict = field(default_factory=dict)  # game_id -> motif du rejet de sa signature


def analyse(messages, room: str, referee_did: str) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    kinds: dict[tuple[str, str], tuple[str, str]] = {}

    def prof(did: str) -> Profile:
        if did not in profiles:
            profiles[did] = Profile(did)
        return profiles[did]

    for msg in sorted(messages, key=lambda m: m.seq):
        if not msg.signed:
            continue
        try:
            data = json.loads(msg.text)
        except ValueError:
            data = None
        if msg.sender == referee_did:
            if classify(msg, room, referee_did) != "receipt" or not isinstance(data, dict):
                continue
            if data.get("type") != "sonnet.receipt.v1":
                continue
            key = (str(data.get("sender_did")), str(data.get("request_id")))
            if key not in kinds:
                continue
            kind, game = kinds[key]
            p = prof(key[0])
            if data.get("status") == "accepted":
                if kind == "withdraw":
                    p.free_by_withdraw = True
                elif kind == "roster":
                    p.free_by_withdraw = False
            else:
                reason = str(data.get("reason", ""))
                if kind == "roster" and ("consent" in reason or "frozen" in reason):
                    p.rejected_on[game] = reason
            continue
        p = prof(msg.sender)
        p.last_activity = max(p.last_activity or 0.0, parse_ts(msg.ts)) if msg.ts else p.last_activity
        if not isinstance(data, dict):
            m = re.search(r"\byes-([a-z0-9][a-z0-9_-]{0,15})", msg.text.lower())
            if m:
                p.applied_to.add(m.group(1))
            continue
        kind = str(data.get("type", ""))
        game = str(data.get("game_id", ""))
        rid = str(data.get("request_id"))
        if kind == "sonnet.roster.v1":
            p.signed_any = True
            kinds[(msg.sender, rid)] = ("roster", game)
        elif kind == "sonnet.withdraw.v1":
            kinds[(msg.sender, rid)] = ("withdraw", game)
        elif kind in ("sonnet.team-request.v1", "sonnet.recruit.v1"):
            p.is_lead = True
        elif kind == "sonnet.application.v1" and game:
            p.applied_to.add(game)
        text = str(data.get("text", "")).lower()
        for m in re.finditer(r"\byes-([a-z0-9][a-z0-9_-]{0,15})", text):
            p.applied_to.add(m.group(1))
    return profiles


def pick_candidates(an: dict[str, Profile], me: str, members: list[str], game_id: str, key_words: dict,
                    now: float, rules: Rules, coverage_required: bool = True) -> list[str]:
    """Writers libres, actifs, pas leads, classes : candidats a notre jeu d'abord, puis les plus recents."""
    out = []
    for did, p in an.items():
        if did == me or did in members or p.is_lead or not ED25519_DID.fullmatch(did):
            continue
        if p.last_activity is None or now - p.last_activity > rules.active_window_s:
            continue
        if p.signed_any and not p.free_by_withdraw:
            continue
        if coverage_required and roster_coverage(members + [did], key_words).missing:
            continue
        out.append(did)
    out.sort(key=lambda d: (game_id not in an[d].applied_to, -(an[d].last_activity or 0)))
    return out


@dataclass
class Replacement:
    old: str
    new: str
    members: list[str]
    reason: str


def decide(status: dict, an: dict[str, Profile], me: str, game_id: str, key_words: dict, now: float,
           rules: Rules, posted_at: float, coverage_required: bool = True,
           blacklist: set[str] | frozenset[str] = frozenset()) -> Replacement | None:
    if status.get("ready") or not status.get("members"):
        return None
    members = list(status["members"])
    pending = [d for d in status.get("pending", []) if d != me]
    if not pending:
        return None
    locked = [d for d in pending if d in blacklist] + \
             [d for d in pending if d not in blacklist and d in an and game_id in an[d].rejected_on and not an[d].free_by_withdraw]
    if locked:
        old = locked[0]
        reason = "membre en liste noire (gele ou non inscrit)" if old in blacklist else f"signature rejetee: {an[old].rejected_on[game_id]}"
    elif now - posted_at >= rules.patience_s:
        old, reason = pending[0], f"pas de signature depuis {int((now - posted_at) // 60)} min"
    else:
        return None
    staying = [d for d in members if d != old]
    cands = [d for d in pick_candidates(an, me, staying, game_id, key_words, now, rules, coverage_required)
             if d not in pending and d not in blacklist]
    if not cands:
        return None
    new = cands[0]
    return Replacement(old, new, [new if d == old else d for d in members], reason)


class RosterManager:
    def __init__(self, client, ident: Identity, referee_did: str, contest_id: str, game_id: str,
                 discovery_room: str, poem_room: str, generation: int, key_words: dict, state_path: Path,
                 rules: Rules | None = None, dry_run: bool = True, coverage_required: bool = True,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 archive=None):
        self.client, self.ident, self.referee_did = client, ident, referee_did
        self.contest_id, self.game_id = contest_id, game_id
        self.discovery_room, self.poem_room, self.generation = discovery_room, poem_room, generation
        self.key_words, self.rules = key_words, rules or Rules()
        self.dry_run, self.coverage_required = dry_run, coverage_required
        self.now, self.sleep, self.archive = now, sleep, archive
        self.state_path = Path(state_path)
        self.state = {"replacements": 0, "counter": 0, "history": [], "failures": 0, "next_attempt_at": 0.0, "blacklist": []}
        self._last_nonce = 0
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text("utf-8")))

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), "utf-8")
        os.replace(tmp, self.state_path)

    def _rid(self, kind: str) -> str:
        """request_id unique par tentative : un id reutilise avec un autre contenu est rejete sans recu."""
        self.state["counter"] = int(self.state.get("counter", 0)) + 1
        return f"xav-{self.game_id}-{kind}-{int(self.now() * 1000)}-{self.state['counter']}"

    def _fail(self, action: str, receipt) -> dict:
        reason = str((receipt or {}).get("reason", "")) if isinstance(receipt, dict) else ""
        if receipt is not None and ("frozen" in reason or "unregistered" in reason):
            # rejet deterministe : on a appris quelque chose (liste noire), on reessaie vite avec un autre candidat
            pause = 300
        else:
            self.state["failures"] = int(self.state.get("failures", 0)) + 1
            pause = min(1800 * 2 ** (self.state["failures"] - 1), 6 * 3600)
        self.state["next_attempt_at"] = self.now() + pause
        self._save()
        log.warning("%s: %s (%s) ; pause %d min avant nouvel essai", self.game_id, action, receipt, pause // 60)
        return {"action": action, "receipt": receipt, "pause_s": pause}

    def _nonce(self) -> int:
        n = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = n
        return n

    def _post(self, text: str):
        res = self.client.say_signed(self.ident, self.discovery_room, text, self._nonce())
        if self.archive:
            self.archive.append(self.discovery_room, {"kind": "manager-post", "text": text, "seq": res.seq})
        return res

    def _await_receipt(self, request_id: str, since: int | None) -> dict | None:
        deadline = self.now() + self.rules.receipt_wait_s
        cursor = since or 0
        while True:
            try:
                page = self.client.read(self.discovery_room, since=cursor)
                for msg in page.messages:
                    if msg.sender != self.referee_did or classify(msg, self.discovery_room, self.referee_did) != "receipt":
                        continue
                    data = json.loads(msg.text)
                    if data.get("type") == "sonnet.receipts.v1":  # recus groupes (souvent des rejets)
                        for r in data.get("receipts", []):
                            if isinstance(r, dict) and r.get("request_id") == request_id and r.get("sender_did") == self.ident.did:
                                return {**r, "status": data.get("status"), "reason": data.get("reason", ""), "batch": True}
                        continue
                    if data.get("request_id") == request_id and data.get("sender_did") == self.ident.did:
                        return data
                if page.last_seq is not None:
                    cursor = max(cursor, int(page.last_seq))
            except RateLimited as e:
                self.sleep(min(e.retry_after, 30))
            except (NetworkError, ApiError, Duplicate) as e:
                log.warning("lecture impossible (%s)", e)
            if self.now() >= deadline:
                return None
            self.sleep(3)

    def invitation(self, act: Replacement, roster_text: str) -> str:
        template = json.loads(roster_text)
        template["request_id"] = "<your unique id>"
        return (f"TEAM {self.game_id}: REVISED ARRAY. Seat ...{act.old[-8:]} released ({act.reason}); "
                f"welcome @{act.new[-8:]}. Room {self.poem_room}, room_generation {self.generation}, referee setup "
                f"accepted, equal split, no fee, we write the poem together, automated agent on my side. "
                f"Members please countersign EXACTLY: {json.dumps(template, separators=(',', ':'))}")

    def run_once(self) -> dict:
        msgs, _ = self.client.export(self.discovery_room)
        st = roster_status(msgs, self.discovery_room, self.referee_did, self.game_id, self.ident.did)
        if st["ready"]:
            return {"action": "ready", "members": st["members"]}
        if not st["members"]:
            return {"action": "no-roster"}
        posted = [m for m in msgs if m.seq == st["roster_seq"]]
        posted_at = parse_ts(posted[0].ts) if posted and posted[0].ts else self.now()
        an = analyse(msgs, self.discovery_room, self.referee_did)
        act = decide(st, an, self.ident.did, self.game_id, self.key_words, self.now(), self.rules, posted_at,
                     self.coverage_required, blacklist=set(self.state.get("blacklist", [])))
        if act is None and self.ident.did not in st["signed"] and self.ident.did in st["members"]:
            act = Replacement(old="", new="", members=list(st["members"]), reason="notre propre consentement manque")
        if act is None:
            return {"action": "wait", "signed": len(st["signed"]), "members": len(st["members"]),
                    "pending": [d[-8:] for d in st["pending"]]}
        log.info("%s: remplacer ...%s par ...%s (%s)", self.game_id, act.old[-8:], act.new[-8:], act.reason)
        if self.dry_run:
            return {"action": "planned", "old": act.old, "new": act.new, "reason": act.reason}
        if self.state["replacements"] >= self.rules.max_replacements:
            log.warning("%s: plafond de remplacements atteint", self.game_id)
            return {"action": "capped"}
        if self.now() < float(self.state.get("next_attempt_at", 0)):
            return {"action": "backoff", "until": self.state["next_attempt_at"]}
        if self.ident.did in st["signed"]:  # notre consentement est actif : le retirer d'abord
            wd_rid = self._rid("wd")
            res = self._post(withdraw_message(self.contest_id, self.game_id, wd_rid))
            rec = self._await_receipt(wd_rid, res.seq)
            if not rec or rec.get("status") != "accepted":
                return self._fail("withdraw-failed", rec)
        roster_rid = self._rid("roster")
        roster_text = roster_message(self.contest_id, self.game_id, self.poem_room, self.generation, act.members, roster_rid)
        res = self._post(roster_text)
        rec = self._await_receipt(roster_rid, res.seq)
        if not rec or rec.get("status") != "accepted":
            reason = str((rec or {}).get("reason", ""))
            if act.new and ("frozen" in reason or "unregistered" in reason):
                bl = set(self.state.get("blacklist", []))
                bl.add(act.new)
                self.state["blacklist"] = sorted(bl)
                log.warning("%s: ...%s mis en liste noire (%s)", self.game_id, act.new[-8:], reason)
            return self._fail("sign-failed", rec)
        self.state["failures"] = 0
        self.state["next_attempt_at"] = 0.0
        if act.old:
            note = self.invitation(act, roster_text)
            refusal = check_reply(note)
            if refusal:
                log.warning("%s: invitation bloquee par le filtre (%s)", self.game_id, refusal)
            else:
                self._post(note)
            self.state["replacements"] += 1
        self.state["history"].append({"at": self.now(), "old": act.old, "new": act.new, "reason": act.reason})
        self._save()
        return {"action": "replaced", "old": act.old, "new": act.new, "members": act.members}

    def run(self, poll_seconds: float, stop=None) -> None:
        while not (stop is not None and stop.is_set()):
            try:
                out = self.run_once()
            except (NetworkError, ApiError, RateLimited) as e:
                log.warning("%s: %s", self.game_id, e)
                out = {"action": "error"}
            if out["action"] == "ready":
                log.info("%s: roster pret, le gestionnaire s'arrete", self.game_id)
                return
            if out["action"] != "wait":
                log.info("%s: %s", self.game_id, json.dumps(out)[:300])
            if stop is not None:
                stop.wait(poll_seconds)
            else:
                self.sleep(poll_seconds)
