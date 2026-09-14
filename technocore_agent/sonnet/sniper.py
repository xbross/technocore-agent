"""Sniper (strategie du 2026-09-14 soir, validee par Xav) : ne viser que les equipes REELLES.

Une equipe est reelle quand au moins `min_core` membres ont un consentement accepte par l'arbitre
sur la meme liste. Si cette liste a un siege muet depuis `silent_s`, on candidate UNE fois, de facon
structuree. Si un signataire accepte de cette equipe reposte une liste qui nous nomme, la
contre-signature est autorisee (politique `core_policy`, branchee sur CounterSigner).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..client import ApiError, Duplicate, NetworkError, RateLimited
from ..identity import Identity
from .lexicon import ED25519_DID
from .watch import classify

log = logging.getLogger("technocore.sonnet.sniper")


def parse_ts(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def team_cores(messages, room: str, referee_did: str) -> dict[str, dict]:
    """Par jeu : derniere liste postee, qui l'a signee avec acceptation, sieges en attente, pret."""
    ordered = sorted(messages, key=lambda m: m.seq)
    posts: dict[tuple[str, str], list[tuple[int, list[str], str, str]]] = {}  # (sender,rid) -> [(seq, members, game, ts)]
    latest: dict[str, tuple[int, list[str], str, str]] = {}  # game -> (seq, members, poster, ts)
    first_poster: dict[tuple[str, tuple[str, ...]], str] = {}  # (game, liste) -> premier a l'avoir postee (le lead)
    for m in ordered:
        if m.sender == referee_did or not m.signed:
            continue
        try:
            d = json.loads(m.text)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "sonnet.roster.v1":
            continue
        game = d.get("game_id")
        members = [x for x in d.get("members", []) if isinstance(x, str) and ED25519_DID.fullmatch(x)]
        if not isinstance(game, str) or not 4 <= len(members) <= 8:
            continue
        posts.setdefault((m.sender, str(d.get("request_id"))), []).append((m.seq, members, game, m.ts))
        first_poster.setdefault((game, tuple(members)), m.sender)
        if game not in latest or m.seq > latest[game][0]:
            latest[game] = (m.seq, members, m.sender, m.ts)
    signed: dict[str, dict[str, list[str]]] = {}  # game -> did -> members signed
    ready: set[str] = set()
    for m in ordered:
        if classify(m, room, referee_did) != "receipt":
            continue
        d = json.loads(m.text)
        if d.get("type") != "sonnet.receipt.v1" or d.get("status") != "accepted":
            continue
        hist = posts.get((str(d.get("sender_did")), str(d.get("request_id"))), [])
        before = [p for p in hist if p[0] < m.seq]
        if not before:
            continue
        _, members, game, _ = before[-1]
        signed.setdefault(game, {})[str(d.get("sender_did"))] = members
        if d.get("roster_ready") is True:
            ready.add(game)
    out: dict[str, dict] = {}
    for game, (seq, members, poster, ts) in latest.items():
        on_latest = [d for d, mem in signed.get(game, {}).items() if mem == members]
        out[game] = {"latest": members, "latest_seq": seq, "latest_ts": ts,
                     "lead": first_poster.get((game, tuple(members)), poster),
                     "signed": on_latest, "pending": [d for d in members if d not in on_latest],
                     "ready": game in ready, "core": set(signed.get(game, {}).keys())}
    return out


def find_openings(cores: dict[str, dict], messages, now: float, min_core: int = 2, silent_s: float = 1800,
                  me: str = "") -> list[dict]:
    last_seen: dict[str, float] = {}
    for m in messages:
        if m.ts:
            last_seen[m.sender] = max(last_seen.get(m.sender, 0.0), parse_ts(m.ts))
    ops = []
    for game, c in cores.items():
        if c["ready"] or me in c["latest"] or len(c["signed"]) < min_core or not c["pending"]:
            continue
        if now - parse_ts(c["latest_ts"]) < silent_s:
            continue
        silent = [d for d in c["pending"] if now - last_seen.get(d, 0.0) >= silent_s]
        if len(silent) != len(c["pending"]):
            continue  # un siege en attente est actif : il va sans doute signer
        ops.append({"game": game, "members": c["latest"], "lead": c["lead"], "silent": silent,
                    "signed": c["signed"], "latest_seq": c["latest_seq"]})
    ops.sort(key=lambda o: (-len(o["signed"]), o["latest_seq"]))
    return ops


def core_policy(cores: dict[str, dict], min_core: int = 2) -> Callable:
    """Politique de contre-signature : le roster doit venir d'un signataire accepte d'une equipe reelle."""
    def policy(msg, data: dict) -> bool:
        c = cores.get(str(data.get("game_id")))
        return bool(c) and len(c["core"]) >= min_core and msg.sender in c["core"]
    return policy


class Sniper:
    def __init__(self, client, ident: Identity, referee_did: str, contest_id: str, discovery_room: str,
                 state_path: Path, dry_run: bool = True, registration_seq: int = 0, letters: str = "",
                 max_per_hour: int = 2, min_core: int = 2, silent_s: float = 1800,
                 now: Callable[[], float] = time.time, archive=None):
        self.client, self.ident, self.referee_did = client, ident, referee_did
        self.contest_id, self.room = contest_id, discovery_room
        self.state_path = Path(state_path)
        self.dry_run, self.registration_seq, self.letters = dry_run, registration_seq, letters
        self.max_per_hour, self.min_core, self.silent_s = max_per_hour, min_core, silent_s
        self.now, self.archive = now, archive
        self.state = {"applied": [], "sent_at": []}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text("utf-8")))
        self.cores: dict[str, dict] = {}
        self._last_nonce = 0

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), "utf-8")
        os.replace(tmp, self.state_path)

    def _nonce(self) -> int:
        n = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = n
        return n

    def application(self, op: dict) -> str:
        silent = " ".join("..." + d[-8:] for d in op["silent"])
        text = (f"Seat offer to {op['game']} from xav: your list seq {op['latest_seq']} has {len(op['signed'])} accepted consents "
                f"and the pending seat {silent} has been silent for a while. I am a registered writer (receipt seq {self.registration_seq}), "
                f"zero live roster consent, automated agent about one word per minute 24/7, letters {self.letters}. "
                f"If you repost the array with my DID in that seat, my agent countersigns within seconds. Non-binding application.")
        return json.dumps({"type": "sonnet.application.v1", "contest_id": self.contest_id, "game_id": op["game"],
                           "no_live_roster_consent": True, "registration_receipt_seq": self.registration_seq,
                           "request_id": f"xav-apply-{op['game']}-{int(self.now() * 1000)}", "text": text},
                          separators=(",", ":"), ensure_ascii=True)

    def run_once(self) -> dict:
        msgs, _ = self.client.export(self.room)
        self.cores = team_cores(msgs, self.room, self.referee_did)
        ops = find_openings(self.cores, msgs, self.now(), self.min_core, self.silent_s, self.ident.did)
        now = self.now()
        self.state["sent_at"] = [t for t in self.state.get("sent_at", []) if now - t < 3600]
        applied = 0
        for op in ops:
            key = f"{op['game']}:{op['latest_seq']}"
            if key in self.state["applied"]:
                continue
            if len(self.state["sent_at"]) >= self.max_per_hour:
                break
            text = self.application(op)
            log.info("%s: siege muet %s, %d consentements acceptes -> candidature%s", op["game"],
                     " ".join(d[-8:] for d in op["silent"]), len(op["signed"]), " (DRY-RUN)" if self.dry_run else "")
            if not self.dry_run:
                res = self.client.say_signed(self.ident, self.room, text, self._nonce())
                if self.archive:
                    self.archive.append(self.room, {"kind": "sniper-post", "text": text, "seq": res.seq})
            self.state["applied"].append(key)
            self.state["sent_at"].append(now)
            applied += 1
        self._save()
        return {"openings": len(ops), "applied": applied, "teams": len(self.cores)}
