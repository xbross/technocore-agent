"""Etat persistant de l'agent : curseurs `since`, derniers nonces, reponses recentes.

Ecriture atomique (fichier temporaire + os.replace) pour qu'une coupure en plein
milieu ne laisse jamais un JSON tronque.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class State:
    path: Path
    cursors: dict[str, int] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    # liste de {"room", "sender", "at"} pour les garde-fous anti-spam
    recent_replies: list[dict] = field(default_factory=list)
    blocked: dict[str, dict] = field(default_factory=dict)  # did -> {"reason", "at"}
    refusals: dict[str, int] = field(default_factory=dict)  # did -> reponses refusees par le filtre
    mailbox: str | None = None  # nom de la room boite aux lettres (mb-p-...)

    @classmethod
    def load(cls, path: Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        data = json.loads(path.read_text("utf-8"))
        return cls(
            path=path,
            cursors={k: int(v) for k, v in data.get("cursors", {}).items()},
            nonces={k: int(v) for k, v in data.get("nonces", {}).items()},
            recent_replies=list(data.get("recent_replies", [])),
            blocked=dict(data.get("blocked", {})),
            refusals={k: int(v) for k, v in data.get("refusals", {}).items()},
            mailbox=data.get("mailbox"),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"cursors": self.cursors, "nonces": self.nonces, "recent_replies": self.recent_replies[-500:],
                   "blocked": self.blocked, "refusals": self.refusals, "mailbox": self.mailbox}
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)

    def next_nonce(self, room: str) -> int:
        """Timestamp ms, strictement superieur au dernier nonce utilise dans cette room."""
        n = max(int(time.time() * 1000), self.nonces.get(room, 0) + 1)
        self.nonces[room] = n
        return n

    def record_reply(self, room: str, sender: str, now: float | None = None) -> None:
        self.recent_replies.append({"room": room, "sender": sender, "at": now or time.time()})

    def replies_in_room_since(self, room: str, seconds: float, now: float | None = None) -> int:
        now = now or time.time()
        return sum(1 for r in self.recent_replies if r["room"] == room and now - r["at"] < seconds)

    def replied_to_sender_since(self, sender: str, seconds: float, now: float | None = None) -> bool:
        now = now or time.time()
        return any(r["sender"] == sender and now - r["at"] < seconds for r in self.recent_replies)

    def block(self, sender: str, reason: str, now: float | None = None) -> None:
        self.blocked[sender] = {"reason": reason, "at": now or time.time()}

    def note_refusal(self, sender: str, threshold: int, reason: str) -> bool:
        """Compte une reponse refusee ; bloque l'emetteur au seuil. Renvoie True s'il vient d'etre bloque."""
        self.refusals[sender] = self.refusals.get(sender, 0) + 1
        if threshold > 0 and self.refusals[sender] >= threshold and sender not in self.blocked:
            self.block(sender, f"{self.refusals[sender]} reponses refusees par le filtre ({reason})")
            return True
        return False
