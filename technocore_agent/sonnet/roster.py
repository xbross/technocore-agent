"""Etat d'un roster : qui a signe la liste exacte qui nous inclut, et le referee a-t-il publie roster_ready."""
from __future__ import annotations

import json

from .watch import classify


def roster_status(messages, room: str, referee_did: str, game_id: str, my_did: str) -> dict:
    """Derniere liste `sonnet.roster.v1` du jeu contenant notre DID, consentements acceptes sur
    cette liste exacte (recus arbitre verifies), et indicateur roster_ready."""
    rosters: dict[tuple[str, str], tuple[list[str], int]] = {}
    latest: list[str] | None = None
    latest_seq = 0
    for msg in sorted(messages, key=lambda m: m.seq):
        if msg.sender == referee_did or not msg.signed:
            continue
        try:
            data = json.loads(msg.text)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("type") != "sonnet.roster.v1" or data.get("game_id") != game_id:
            continue
        members = [m for m in data.get("members", []) if isinstance(m, str)]
        rosters[(msg.sender, str(data.get("request_id")))] = (members, msg.seq)
        if my_did in members and msg.seq > latest_seq:
            latest, latest_seq = members, msg.seq
    signed: list[str] = []
    ready = False
    if latest is not None:
        for msg in sorted(messages, key=lambda m: m.seq):
            if classify(msg, room, referee_did) != "receipt":
                continue
            data = json.loads(msg.text)
            if data.get("type") != "sonnet.receipt.v1" or data.get("status") != "accepted":
                continue
            entry = rosters.get((str(data.get("sender_did")), str(data.get("request_id"))))
            if entry and entry[0] == latest:
                if data["sender_did"] not in signed:
                    signed.append(data["sender_did"])
                if data.get("roster_ready") is True:
                    ready = True
    return {"game_id": game_id, "members": latest or [], "signed": signed,
            "pending": [m for m in (latest or []) if m not in signed], "ready": ready, "roster_seq": latest_seq}


def wait_for_roster(fetch_status, sleep=None, poll_seconds: float = 30.0, stop=None) -> dict | None:
    """Interroge fetch_status() jusqu'a roster_ready. Journalise chaque changement de liste ou de
    signataires (la liste peut etre revisee par le lead : nos signatures anterieures ne comptent plus)."""
    import logging
    import time
    log = logging.getLogger("technocore.sonnet.roster")
    sleep = sleep or time.sleep
    last = None
    while not (stop is not None and stop.is_set()):
        st = fetch_status()
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
