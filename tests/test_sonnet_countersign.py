"""Contre-signature automatique, strictement limitee : un lead precis, un jeu precis, notre DID dans la liste."""
import json

from technocore_agent import identity
from technocore_agent.client import Message, RoomPage, SayResult
from technocore_agent.sonnet import countersign, team

DISC = "mb-sonnet-2-discovery"


class FakeClient:
    def __init__(self, ref):
        self.ref = ref
        self.msgs = []
        self.said = []
        self.seq = 0

    def add(self, ident, text):
        self.seq += 1
        self.msgs.append(Message(self.seq, "t", ident.did, text, self.seq, ident.sign(DISC, self.seq, text)))
        return self.seq

    def read(self, room, since=None, limit=200, wait=None):
        new = [m for m in self.msgs if m.seq > (since or 0)]
        return RoomPage(room, new[0].seq if new else None, new[-1].seq if new else None, new, generation=1)

    def say_signed(self, ident, room, text, nonce):
        self.said.append(text)
        seq = self.add(ident, text)
        data = json.loads(text)
        rec = {"type": "sonnet.receipt.v1", "status": "accepted", "request_id": data["request_id"],
               "sender_did": ident.did, "roster_ready": False}
        self.add(self.ref, json.dumps(rec))
        return SayResult("", seq, True)


def _setup(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    me = identity.create(tmp_path / "me.pem", "pw")
    lead = identity.create(tmp_path / "lead.pem", "pw")
    other = identity.create(tmp_path / "other.pem", "pw")
    return ref, me, lead, other, FakeClient(ref)


def test_signs_only_a_lead_roster_for_the_game_that_names_us(tmp_path):
    ref, me, lead, other, client = _setup(tmp_path)
    members = [lead.did, me.did] + ["did:key:z6Mk" + c * 44 for c in "ab"]
    client.add(other, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, members, "o-1"))  # pas le lead
    client.add(lead, team.roster_message("sonnet-2", "h6", "d-sonnet-2-team-h6", 1, members, "l-0"))   # autre jeu
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, members[:1] + members[2:] + ["did:key:z6Mk" + "c" * 44], "l-1"))  # sans nous
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id="h5",
                                  lead_did=lead.did, discovery_room=DISC, dry_run=False, sleep=lambda s: None,
                                  now=iter(range(0, 10000)).__next__)
    w.step()
    assert client.said == []
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, members, "l-2"))
    w.step()
    assert len(client.said) == 1
    signed = json.loads(client.said[0])
    assert signed["members"] == members and signed["poem_room"] == "d-sonnet-2-team-h5" and signed["room_generation"] == 1
    assert w.signed_members == members


def test_revised_lead_roster_triggers_release_then_new_signature(tmp_path):
    ref, me, lead, other, client = _setup(tmp_path)
    members = [lead.did, me.did] + ["did:key:z6Mk" + c * 44 for c in "ab"]
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, members, "l-1"))
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id="h5",
                                  lead_did=lead.did, discovery_room=DISC, dry_run=False, sleep=lambda s: None,
                                  now=iter(range(0, 10000)).__next__)
    w.step()
    revised = [lead.did, me.did, other.did, members[3]]
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, revised, "l-2"))
    w.step()
    types = [json.loads(t)["type"] for t in client.said]
    assert types == ["sonnet.roster.v1", "sonnet.withdraw.v1", "sonnet.roster.v1"]
    assert json.loads(client.said[-1])["members"] == revised


def test_dry_run_never_writes_and_rejects_bad_sizes(tmp_path):
    ref, me, lead, other, client = _setup(tmp_path)
    too_small = [lead.did, me.did, other.did]
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, too_small + ["did:key:z6Mk" + "a" * 44], "l-1"))
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id="h5",
                                  lead_did=lead.did, discovery_room=DISC, dry_run=True, sleep=lambda s: None,
                                  now=iter(range(0, 10000)).__next__)
    out = w.step()
    assert client.said == [] and out["action"] == "planned"


def test_a_roster_whose_signature_failed_is_not_retried_every_step(tmp_path):
    ref, me, lead, other, client = _setup(tmp_path)
    members = [lead.did, me.did] + ["did:key:z6Mk" + c * 44 for c in "ab"]

    class RejectingClient(FakeClient):
        def say_signed(self, ident, room, text, nonce):
            self.said.append(text)
            seq = self.add(ident, text)
            data = json.loads(text)
            self.add(self.ref, json.dumps({"type": "sonnet.receipt.v1", "status": "rejected", "reason": "consent: withdraw before changing",
                                           "request_id": data["request_id"], "sender_did": ident.did}))
            return SayResult("", seq, True)

    client = RejectingClient(ref)
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, members, "l-1"))
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id="h5",
                                  lead_did=lead.did, discovery_room=DISC, dry_run=False, sleep=lambda s: None,
                                  now=iter(range(0, 10000)).__next__)
    assert w.step()["action"] == "sign-failed"
    assert w.step()["action"] == "wait" and len(client.said) == 1  # pas de nouvelle tentative sur la meme liste
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, members, "l-1-repost"))
    assert w.step()["action"] == "wait" and len(client.said) == 1  # ni sur un repost identique


def test_multiple_leads_any_game_and_release_of_our_own_team_first(tmp_path):
    ref, me, lead, other, client = _setup(tmp_path)
    lead2 = identity.create(tmp_path / "lead2.pem", "pw")
    members = [lead2.did, me.did] + ["did:key:z6Mk" + c * 44 for c in "ab"]
    client.add(lead2, team.roster_message("sonnet-2", "nohitori-3", "d-sonnet-2-team-nohitori-3", 2, members, "l2-1"))
    hold = tmp_path / "hold.json"
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id=None,
                                  lead_dids={lead.did, lead2.did}, discovery_room=DISC, dry_run=False,
                                  release_game="rimbaud-gang", hold_path=hold, sleep=lambda s: None,
                                  now=iter(range(0, 10000)).__next__)
    out = w.step()
    types = [json.loads(t)["type"] for t in client.said]
    assert types == ["sonnet.withdraw.v1", "sonnet.roster.v1"]
    assert json.loads(client.said[0])["game_id"] == "rimbaud-gang"
    signed = json.loads(client.said[1])
    assert signed["game_id"] == "nohitori-3" and signed["room_generation"] == 2 and out["action"] == "signed"
    assert json.loads(hold.read_text())["game"] == "nohitori-3"


def test_roster_from_unknown_lead_or_wrong_room_is_ignored(tmp_path):
    ref, me, lead, other, client = _setup(tmp_path)
    members = [other.did, me.did] + ["did:key:z6Mk" + c * 44 for c in "ab"]
    client.add(other, team.roster_message("sonnet-2", "g1", "d-sonnet-2-team-g1", 1, members, "o-1"))
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id=None,
                                  lead_dids={lead.did}, discovery_room=DISC, dry_run=False, sleep=lambda s: None,
                                  now=iter(range(0, 10000)).__next__)
    assert w.step()["action"] == "wait" and client.said == []
