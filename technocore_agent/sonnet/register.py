"""Inscription au concours : une ecriture signee, puis attente du recu signe par l'arbitre.

Regles (sonnet-game.md, paquet epingle) : la premiere inscription acceptee fige le role et le
DID ; meme (contest_id, signataire, request_id) => le referee renvoie le recu d'origine.
Verrous : SonnetConfig.armed doit etre vrai, et l'appelant doit avoir la validation humaine.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

from ..client import Duplicate, NetworkError, RateLimited, ApiError
from ..identity import Identity
from .config import SonnetConfig
from .watch import classify

log = logging.getLogger("technocore.sonnet.register")


class Disarmed(Exception):
    pass


def registration_message(cfg: SonnetConfig, request_id: str) -> str:
    body = {"type": "sonnet.register.v1", "contest_id": cfg.contest_id, "role": cfg.role}
    if cfg.role == "writer":
        if not cfg.x_account_url:
            raise ValueError("participant.x_account_url est obligatoire pour le role writer")
        body["x_account_url"] = cfg.x_account_url
    body["request_id"] = request_id
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True)


def find_receipt(page_messages, room: str, referee_did: str, my_did: str, request_id: str) -> dict | None:
    """Le seul recu qui compte : signe par l'arbitre, pour notre DID et notre request_id."""
    for msg in page_messages:
        if classify(msg, room, referee_did) != "receipt":
            continue
        data = json.loads(msg.text)
        if data.get("type") == "sonnet.receipt.v1" and data.get("sender_did") == my_did \
                and data.get("request_id") == request_id:
            data["_seq"] = msg.seq
            data["_ts"] = msg.ts
            return data
    return None


def register(client, ident: Identity, cfg: SonnetConfig, request_id: str, wait_seconds: float = 300,
             sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time,
             poll_seconds: float = 2.0) -> dict | None:
    """Poste l'inscription UNE fois et attend le recu. None si aucun recu dans le delai
    (relancer avec le meme request_id : le referee renvoie alors le recu d'origine)."""
    if not cfg.armed:
        raise Disarmed("participant.armed = false dans sonnet.toml : aucune ecriture autorisee")
    room = cfg.rooms["registration"]
    text = registration_message(cfg, request_id)
    nonce = int(now() * 1000)
    log.info("inscription %s role=%s request_id=%s nonce=%d", cfg.contest_id, cfg.role, request_id, nonce)
    result = client.say_signed(ident, room, text, nonce)
    log.info("ecriture acceptee par le serveur: seq=%s verifie=%s", result.seq, result.verified)
    cursor = result.seq or 0
    deadline = now() + wait_seconds
    while True:
        try:
            page = client.read(room, since=cursor)
            receipt = find_receipt(page.messages, room, cfg.referee_did, ident.did, request_id)
            if receipt:
                log.info("recu arbitre seq=%s status=%s reason=%r", receipt["_seq"], receipt.get("status"),
                         receipt.get("reason"))
                return receipt
            if page.first_seq is not None and cursor and page.first_seq > cursor + 1:
                log.warning("%d lignes manquees entre deux lectures ; le recu peut etre passe", page.first_seq - cursor - 1)
            if page.last_seq is not None:
                cursor = max(cursor, int(page.last_seq))
        except RateLimited as e:
            log.warning("limite de debit, pause %.0fs", e.retry_after)
            sleep(min(e.retry_after, 30))
        except (NetworkError, ApiError, Duplicate) as e:
            log.warning("lecture impossible (%s), nouvel essai", e)
        if now() >= deadline:
            log.warning("aucun recu pour request_id=%s en %.0fs", request_id, wait_seconds)
            return None
        sleep(poll_seconds)
