"""Configuration via variables d'environnement, avec lecture optionnelle d'un fichier .env.

Aucune dependance : le .env est lu ligne par ligne (CLE=valeur, # commentaires).
Les variables deja presentes dans l'environnement gagnent sur le .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    home: Path
    rooms: list[str]
    nick: str
    poll_seconds: float
    note_extra: str
    max_replies_per_room_per_hour: int
    max_replies_per_hour: int
    sender_cooldown_seconds: float
    model: str | None
    base_url: str
    history_window: int = 600  # textes recents gardes par room pour le filtre anti-boilerplate
    max_candidates_per_poll: int = 25  # lignes (les plus recentes) soumises au cerveau par room et par tour

    @property
    def key_path(self) -> Path:
        return self.home / "identity" / "agent_key.pem"

    @property
    def state_path(self) -> Path:
        return self.home / "state" / "state.json"

    @property
    def log_path(self) -> Path:
        return self.home / "logs" / "agent.log"

    @classmethod
    def from_env(cls) -> "Config":
        home = Path(os.environ.get("TECHNOCORE_HOME", PROJECT_DIR)).expanduser()
        load_dotenv(home / ".env")
        load_dotenv(PROJECT_DIR / ".env")
        rooms = [r.strip() for r in os.environ.get("TECHNOCORE_ROOMS", "lobby,technocore,meta").split(",") if r.strip()]
        return cls(
            home=home,
            rooms=rooms,
            nick=os.environ.get("TECHNOCORE_NICK", "agent").strip().lower(),
            poll_seconds=float(os.environ.get("TECHNOCORE_POLL_SECONDS", "30")),
            note_extra=os.environ.get("TECHNOCORE_NOTE_EXTRA", "").strip(),
            max_replies_per_room_per_hour=int(os.environ.get("TECHNOCORE_MAX_REPLIES_PER_ROOM_HOUR", "8")),
            max_replies_per_hour=int(os.environ.get("TECHNOCORE_MAX_REPLIES_PER_HOUR", "20")),
            sender_cooldown_seconds=float(os.environ.get("TECHNOCORE_SENDER_COOLDOWN_SECONDS", "600")),
            model=os.environ.get("TECHNOCORE_MODEL") or None,
            base_url=os.environ.get("TECHNOCORE_BASE_URL", "https://technocore.chat"),
            max_candidates_per_poll=int(os.environ.get("TECHNOCORE_MAX_CANDIDATES_PER_POLL", "25")),
        )
