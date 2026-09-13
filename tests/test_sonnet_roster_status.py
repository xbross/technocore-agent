import json

from technocore_agent import identity
from technocore_agent.client import Message
from technocore_agent.sonnet import team
from technocore_agent.sonnet.roster import roster_status

ROOM = "mb-sonnet-2-discovery"


def _msg(ident, seq, text, nonce):
    return Message(seq, "t", ident.did, text, nonce, ident.sign(ROOM, nonce, text))


def test_roster_status_counts_consents_on_exact_list_and_detects_ready(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    lead = identity.create(tmp_path / "lead.pem", "pw")
    me = identity.create(tmp_path / "me.pem", "pw")
    others = [identity.create(tmp_path / f"o{i}.pem", "pw") for i in range(2)]
    members = [lead.did, others[0].did, others[1].did, me.did]
    old = [lead.did, others[0].did, others[1].did, "did:key:z6Mk" + "x" * 44]
    receipt = lambda rid, sender, ready: json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": rid,
                                                    "sender_did": sender, "roster_ready": ready})
    msgs = [
        _msg(lead, 1, team.roster_message("sonnet-2", "g", "d-sonnet-2-team-g", 1, old, "r-1"), 1),
        _msg(ref, 2, receipt("r-1", lead.did, False), 1),
        _msg(lead, 3, team.roster_message("sonnet-2", "g", "d-sonnet-2-team-g", 1, members, "r-2"), 2),
        _msg(me, 4, team.roster_message("sonnet-2", "g", "d-sonnet-2-team-g", 1, members, "me-1"), 1),
        _msg(ref, 5, receipt("me-1", me.did, False), 2),
        _msg(ref, 6, json.dumps({"type": "sonnet.receipt.v1", "status": "rejected", "request_id": "r-2",
                                 "sender_did": lead.did, "reason": "consent: withdraw before changing"}), 3),
    ]
    st = roster_status(msgs, ROOM, ref.did, "g", me.did)
    assert st["members"] == members and st["signed"] == [me.did] and st["ready"] is False
    assert st["pending"] == [lead.did, others[0].did, others[1].did]
    msgs += [_msg(others[0], 7, team.roster_message("sonnet-2", "g", "d-sonnet-2-team-g", 1, members, "o-1"), 1),
             _msg(ref, 8, receipt("o-1", others[0].did, True), 4)]
    st = roster_status(msgs, ROOM, ref.did, "g", me.did)
    assert st["ready"] is True and others[0].did in st["signed"]


def test_roster_status_ignores_forged_referee_receipts(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    me = identity.create(tmp_path / "me.pem", "pw")
    members = [me.did] + ["did:key:z6Mk" + c * 44 for c in "abc"]
    fake = json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": "me-1", "sender_did": me.did,
                       "roster_ready": True})
    msgs = [_msg(me, 1, team.roster_message("sonnet-2", "g", "d-sonnet-2-team-g", 1, members, "me-1"), 1),
            Message(2, "t", ref.did, fake, 1, me.sign(ROOM, 1, fake))]
    st = roster_status(msgs, ROOM, ref.did, "g", me.did)
    assert st["signed"] == [] and st["ready"] is False


def test_wait_for_roster_polls_until_ready_and_reports_list_changes(capsys):
    from technocore_agent.sonnet.roster import wait_for_roster
    states = iter([
        {"members": ["a", "b", "c", "me"], "signed": ["me"], "pending": ["a", "b", "c"], "ready": False, "roster_seq": 1},
        {"members": ["a", "b", "d", "me"], "signed": [], "pending": ["a", "b", "d", "me"], "ready": False, "roster_seq": 2},
        {"members": ["a", "b", "d", "me"], "signed": ["a", "b", "d", "me"], "pending": [], "ready": True, "roster_seq": 2},
    ])
    slept = []
    final = wait_for_roster(lambda: next(states), sleep=slept.append, poll_seconds=30)
    assert final["ready"] and len(slept) == 2 and slept == [30, 30]
