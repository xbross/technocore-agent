"""Etat d'un roster : qui a signe la liste exacte qui nous inclut, et le referee a-t-il publie roster_ready."""
from __future__ import annotations

import json

from .watch import classify


def roster_status(messages, room: str, referee_did: str, game_id: str, my_did: str) -> dict:
    """Derniere liste `sonnet.roster.v1` du jeu contenant notre DID, consentements acceptes sur
    cette liste exacte (recus arbitre verifies), et indicateur roster_ready.

    Un recu est attribue au DERNIER post (roster ou retrait) du meme signataire portant le meme
    request_id et anterieur au recu : un request_id reutilise avec une autre liste ne recupere
    donc pas l'ancien recu. Un retrait accepte apres la signature annule le consentement."""
    ordered = sorted(messages, key=lambda m: m.seq)
    posts: dict[tuple[str, str], list[tuple[int, str, list[str], str]]] = {}
    latest: list[str] | None = None
    latest_seq = 0
    for msg in ordered:
        if msg.sender == referee_did or not msg.signed:
            continue
        try:
            data = json.loads(msg.text)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("game_id") != game_id:
            continue
        kind = data.get("type")
        if kind == "sonnet.roster.v1":
            members = [m for m in data.get("members", []) if isinstance(m, str)]
            posts.setdefault((msg.sender, str(data.get("request_id"))), []).append((msg.seq, "roster", members, game_id))
            if my_did in members and msg.seq > latest_seq:
                latest, latest_seq = members, msg.seq
        elif kind == "sonnet.withdraw.v1":
            posts.setdefault((msg.sender, str(data.get("request_id"))), []).append((msg.seq, "withdraw", [], game_id))
    signed_at: dict[str, int] = {}
    ready = False
    if latest is not None:
        for msg in ordered:
            if classify(msg, room, referee_did) != "receipt":
                continue
            data = json.loads(msg.text)
            if data.get("type") != "sonnet.receipt.v1" or data.get("status") != "accepted":
                continue
            sender = str(data.get("sender_did"))
            history = posts.get((sender, str(data.get("request_id"))), [])
            before = [p for p in history if p[0] < msg.seq]
            if not before:
                continue
            _, kind, members, _ = before[-1]
            if kind == "roster" and members == latest:
                signed_at[sender] = msg.seq
                if data.get("roster_ready") is True:
                    ready = True
            elif kind == "withdraw":
                signed_at.pop(sender, None)
    signed = [d for d in (latest or []) if d in signed_at]
    return {"game_id": game_id, "members": latest or [], "signed": signed,
            "pending": [m for m in (latest or []) if m not in signed_at], "ready": ready, "roster_seq": latest_seq}


def wait_for_roster(fetch_status, sleep=None, poll_seconds: float = 30.0, stop=None) -> dict | None:
    """Interroge fetch_status() jusqu'a roster_ready. Journalise chaque changement de liste ou de
    signataires (la liste peut etre revisee par le lead : nos signatures anterieures ne comptent plus)."""
    import logging
    import time
    log = logging.getLogger("technocore.sonnet.roster")
    sleep = sleep or time.sleep
    last = None
    from ..client import ApiError, NetworkError, RateLimited
    while not (stop is not None and stop.is_set()):
        try:
            st = fetch_status()
        except (NetworkError, ApiError, RateLimited) as e:
            log.warning("etat du roster illisible (%s), nouvel essai", e)
            sleep(poll_seconds)
            continue
        key = (tuple(st.get("members", [])), tuple(st.get("signed", [])), bool(st.get("ready")))
        if key != last:
            log.info("roster %s: liste seq %s, %d/%d signatures acceptees, ready=%s, en attente: %s",
                     st.get("game_id", "?"), st.get("roster_seq"), len(st.get("signed", [])),
                     len(st.get("members", [])), st.get("ready"), " ".join(m[-8:] for m in st.get("pending", [])))
            last = key
        if st.get("ready"):
            return st
        sleep(poll_seconds)
    return None
