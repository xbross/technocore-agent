"""Messages de formation d'equipe (sonnet-game.md, section Teams and identity / Rooms and signed protocol)."""
from __future__ import annotations

import json
import re

from .lexicon import ED25519_DID

GAME_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")


def _compact(body: dict) -> str:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True)


def check_game_id(game_id: str) -> str:
    if not isinstance(game_id, str) or not GAME_ID.match(game_id):
        raise ValueError("game_id: 1-16 caracteres [a-z0-9_-], commence par une lettre ou un chiffre")
    return game_id


def team_request_message(contest_id: str, game_id: str, request_id: str) -> str:
    return _compact({"type": "sonnet.team-request.v1", "contest_id": contest_id,
                     "game_id": check_game_id(game_id), "request_id": request_id})


def roster_message(contest_id: str, game_id: str, poem_room: str, room_generation: int,
                   members: list[str], request_id: str) -> str:
    if not 4 <= len(members) <= 8:
        raise ValueError("roster: 4 a 8 membres")
    if len(set(members)) != len(members):
        raise ValueError("roster: DID en double")
    for m in members:
        if not ED25519_DID.fullmatch(m):
            raise ValueError(f"roster: DID invalide {m[:20]}...")
    if poem_room != f"d-sonnet-2-team-{game_id}" and not poem_room.endswith(f"-team-{game_id}"):
        raise ValueError("roster: poem_room ne correspond pas au game_id")
    return _compact({"type": "sonnet.roster.v1", "contest_id": contest_id, "game_id": check_game_id(game_id),
                     "poem_room": poem_room, "room_generation": int(room_generation),
                     "members": list(members), "request_id": request_id})


def withdraw_message(contest_id: str, game_id: str, request_id: str) -> str:
    return _compact({"type": "sonnet.withdraw.v1", "contest_id": contest_id,
                     "game_id": check_game_id(game_id), "request_id": request_id})
