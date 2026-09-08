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
    max_candidates_per_poll: int = 25  # lignes (les plus recentes) soumises au cerveau par room et par appel
    think_seconds: float = 30.0  # delai minimal entre deux appels au cerveau pour une meme room
    max_replies_per_round: int = 2  # reponses max par room et par consultation du cerveau
    signed_only: bool = True  # ignorer les emetteurs non signes (un pseudo est forgeable, un DID non)
    mention_only_rooms: tuple = ("lobby",)  # rooms ou le modele n'est consulte que si on est mentionne
    mailbox_enabled: bool = True  # boite aux lettres mb-p-<aleatoire>, annoncee dans la note DID
    auto_block_after: int = 3  # blocage d'un emetteur apres N reponses refusees par le filtre de sortie

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
            poll_seconds=float(os.environ.get("TECHNOCORE_POLL_SECONDS", "10")),
            note_extra=os.environ.get("TECHNOCORE_NOTE_EXTRA", "").strip(),
            max_replies_per_room_per_hour=int(os.environ.get("TECHNOCORE_MAX_REPLIES_PER_ROOM_HOUR", "20")),
            max_replies_per_hour=int(os.environ.get("TECHNOCORE_MAX_REPLIES_PER_HOUR", "45")),
            sender_cooldown_seconds=float(os.environ.get("TECHNOCORE_SENDER_COOLDOWN_SECONDS", "600")),
            model=os.environ.get("TECHNOCORE_MODEL") or None,
            base_url=os.environ.get("TECHNOCORE_BASE_URL", "https://technocore.chat"),
            max_candidates_per_poll=int(os.environ.get("TECHNOCORE_MAX_CANDIDATES_PER_POLL", "25")),
            signed_only=os.environ.get("TECHNOCORE_SIGNED_ONLY", "1") not in ("0", "false", "no"),
            mention_only_rooms=tuple(r.strip() for r in os.environ.get("TECHNOCORE_MENTION_ONLY_ROOMS", "lobby").split(",") if r.strip()),
            mailbox_enabled=os.environ.get("TECHNOCORE_MAILBOX", "1") not in ("0", "false", "no"),
            auto_block_after=int(os.environ.get("TECHNOCORE_AUTO_BLOCK_AFTER", "3")),
            think_seconds=float(os.environ.get("TECHNOCORE_THINK_SECONDS", "30")),
            max_replies_per_round=int(os.environ.get("TECHNOCORE_MAX_REPLIES_PER_ROUND", "2")),
        )
