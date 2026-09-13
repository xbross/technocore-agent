"""Configuration du concours, separee du code (sonnet.toml)."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .lexicon import ED25519_DID

X_URL = re.compile(r"^https://x\.com/[A-Za-z0-9_]{1,15}$")


class ConfigError(Exception):
    pass


@dataclass
class SonnetConfig:
    path: Path
    contest_id: str
    referee_did: str
    deadline: str
    rooms: dict[str, str]
    package_dir: Path
    package_commit: str
    package_sha256: dict[str, str]
    role: str
    x_account_url: str | None
    did: str | None
    armed: bool
    poll_seconds: float
    archive_dir: Path
    state_path: Path
    model: str
    effort: str
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SonnetConfig":
        path = Path(path)
        try:
            data = tomllib.loads(path.read_text("utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"{path}: {e}") from e
        base = path.parent
        contest = data.get("contest", {})
        referee = str(contest.get("referee_did", ""))
        if not ED25519_DID.fullmatch(referee):
            raise ConfigError("contest.referee_did: DID did:key:z6Mk... attendu (voir LAUNCH.md officiel)")
        part = data.get("participant", {})
        x_url = part.get("x_account_url")
        if x_url is not None and not X_URL.match(str(x_url)):
            raise ConfigError("participant.x_account_url: format attendu https://x.com/<handle>")
        did = part.get("did")
        if did is not None and not ED25519_DID.fullmatch(str(did)):
            raise ConfigError("participant.did: DID did:key:z6Mk... attendu")
        pkg = data.get("package", {})
        watch = data.get("watch", {})
        brain = data.get("brain", {})
        role = str(part.get("role", "writer"))
        if role not in ("writer", "voter", "organizer"):
            raise ConfigError("participant.role: writer, voter ou organizer")
        return cls(
            path=path,
            contest_id=str(contest.get("id", "sonnet-2")),
            referee_did=referee,
            deadline=str(contest.get("deadline", "")),
            rooms={str(k): str(v) for k, v in data.get("rooms", {}).items()},
            package_dir=base / str(pkg.get("dir", "contest/package")),
            package_commit=str(pkg.get("commit", "")),
            package_sha256={str(k): str(v) for k, v in pkg.get("sha256", {}).items()},
            role=role,
            x_account_url=str(x_url) if x_url is not None else None,
            did=str(did) if did is not None else None,
            armed=bool(part.get("armed", False)),
            poll_seconds=float(watch.get("poll_seconds", 15)),
            archive_dir=base / str(watch.get("archive_dir", "state/sonnet-archive")),
            state_path=base / str(watch.get("state_path", "state/sonnet_watch.json")),
            model=str(brain.get("model", "opus")),
            effort=str(brain.get("effort", "medium")),
            extra=data,
        )
