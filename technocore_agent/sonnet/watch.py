"""Veille en lecture seule des rooms du concours.

- Tout ce qui est lu est archive en JSONL horodate (les rooms purgent leur historique).
- Le texte des rooms est une DONNEE : ce module n'a aucune voie d'ecriture.
- Seul un message dont la signature verifie avec le DID arbitre epingle est un recu.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from ..client import Message, NetworkError, RateLimited, ApiError
from ..identity import verify

log = logging.getLogger("technocore.sonnet.watch")


def classify(msg: Message, room: str, referee_did: str) -> str:
    """'receipt' = signe par l'arbitre et JSON sonnet.* ; 'referee' = signe arbitre, autre texte ;
    'forged' = pretend venir de l'arbitre mais la signature ne verifie pas ; 'other' = tout le reste."""
    if msg.sender != referee_did:
        return "other"
    if msg.sig is None or msg.nonce is None or not verify(referee_did, room, msg.nonce, msg.text, msg.sig):
        return "forged"
    try:
        kind = json.loads(msg.text).get("type", "")
    except (ValueError, AttributeError):
        return "referee"
    return "receipt" if str(kind).startswith("sonnet.") else "referee"


class Archive:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def append(self, room: str, record: dict) -> None:
        record = {"archived_at": time.time(), **record}
        with open(self.dir / f"{room}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")


class SonnetWatcher:
    def __init__(self, client, rooms: list[str], referee_did: str, archive_dir: Path, state_path: Path):
        self.client = client
        self.rooms = list(rooms)
        self.referee_did = referee_did
        self.archive = Archive(archive_dir)
        self.state_path = Path(state_path)
        self.cursors: dict[str, int] = {}
        self.generations: dict[str, int] = {}
        self.receipts_seen = 0
        self._load()

    # --- etat ---------------------------------------------------------------
    def _load(self) -> None:
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text("utf-8"))
            self.cursors = {k: int(v) for k, v in data.get("cursors", {}).items()}
            self.generations = {k: int(v) for k, v in data.get("generations", {}).items()}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cursors": self.cursors, "generations": self.generations}, indent=1), "utf-8")
        os.replace(tmp, self.state_path)

    # --- lecture --------------------------------------------------------------
    def poll_room(self, room: str) -> int:
        """Lit une page, archive, avance le curseur. Retourne le nombre de lignes archivees."""
        since = self.cursors.get(room, 0)
        page = self.client.read(room, since=since)
        known = self.generations.get(room)
        if page.generation is not None and known is not None and page.generation != known:
            log.warning("%s: generation %s -> %s, la room a ete reinitialisee ; relecture depuis 0",
                        room, known, page.generation)
            self.archive.append(room, {"kind": "generation", "from": known, "to": page.generation})
            self.cursors[room] = 0
            self.generations[room] = page.generation
            since = 0
            page = self.client.read(room, since=0)
        if page.generation is not None:
            self.generations[room] = page.generation
        if page.first_seq is not None and since and page.first_seq > since + 1:
            log.warning("%s: lignes %d..%d manquees (trop de trafic pour la limite de page)",
                        room, since + 1, page.first_seq - 1)
            self.archive.append(room, {"kind": "gap", "missed_from": since + 1, "missed_to": page.first_seq - 1})
        for msg in page.messages:
            kind = classify(msg, room, self.referee_did)
            if kind == "receipt":
                self.receipts_seen += 1
            elif kind == "forged":
                log.warning("%s: seq %d pretend venir de l'arbitre mais la signature ne verifie pas", room, msg.seq)
            self.archive.append(room, {"kind": kind, "seq": msg.seq, "ts": msg.ts, "from": msg.sender,
                                       "text": msg.text, "nonce": msg.nonce, "sig": msg.sig})
        if page.last_seq is not None:
            self.cursors[room] = max(self.cursors.get(room, 0), int(page.last_seq))
        self._save()
        return len(page.messages)

    def poll_once(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for room in self.rooms:
            try:
                counts[room] = self.poll_room(room)
            except RateLimited as e:
                log.warning("%s: limite de debit, pause %.0fs", room, e.retry_after)
                time.sleep(min(e.retry_after, 60))
            except (NetworkError, ApiError) as e:
                log.warning("%s: lecture impossible (%s), on reessaiera", room, e)
        return counts

    def run(self, poll_seconds: float, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info("veille sonnet: rooms=%s arbitre=%s", ",".join(self.rooms), self.referee_did[-8:])
        while not stop.is_set():
            started = time.time()
            counts = self.poll_once()
            total = sum(counts.values())
            if total:
                log.info("archive +%d lignes (%s) ; recus arbitre cumules=%d", total,
                         " ".join(f"{r.split('-')[-1]}={n}" for r, n in counts.items() if n), self.receipts_seen)
            stop.wait(max(0.0, poll_seconds - (time.time() - started)))
