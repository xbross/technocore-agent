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


class LateClient(FakeClient):
    """Arbitre en retard : les posts sont enregistres mais aucun recu n'arrive tant que deliver() n'est pas appele."""

    def __init__(self, ref):
        super().__init__(ref)
        self.late = False

    def say_signed(self, ident, room, text, nonce):
        if not self.late:
            return super().say_signed(ident, room, text, nonce)
        self.said.append(text)
        return SayResult("", self.add(ident, text), True)

    def deliver(self, ident, rid, status="accepted", reason=""):
        self.add(self.ref, json.dumps({"type": "sonnet.receipt.v1", "status": status, "reason": reason,
                                       "request_id": rid, "sender_did": ident.did, "roster_ready": False}))


def _signed_then_revised_with_late_referee(tmp_path):
    ref, me, lead, other, client = _setup(tmp_path)
    client = LateClient(ref)
    members = [lead.did, me.did] + ["did:key:z6Mk" + c * 44 for c in "ab"]
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, members, "l-1"))
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id="h5",
                                  lead_did=lead.did, discovery_room=DISC, dry_run=False, sleep=lambda s: None,
                                  now=iter(range(0, 100000)).__next__, receipt_wait_s=10)
    assert w.step()["action"] == "signed"
    client.late = True
    revised = [lead.did, me.did, other.did, members[3]]
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, revised, "l-2"))
    return me, client, w, revised


def test_late_referee_receipt_keeps_the_revised_list_pending_without_reposting(tmp_path):
    """Retard de l'arbitre (~90 min observes le 15/09) : retrait et re-signature partent a la suite, sans attendre
    le recu du retrait, et l'absence de recu ne condamne pas la liste ; rien n'est reposte entre-temps."""
    me, client, w, revised = _signed_then_revised_with_late_referee(tmp_path)
    out = w.step()
    types = [json.loads(t)["type"] for t in client.said]
    assert types == ["sonnet.roster.v1", "sonnet.withdraw.v1", "sonnet.roster.v1"]
    assert out["action"] == "pending" and json.loads(client.said[-1])["members"] == revised
    assert w.step()["action"] == "pending" and len(client.said) == 3
    assert w.step()["action"] == "pending" and len(client.said) == 3


def test_pending_list_is_confirmed_when_the_late_receipt_finally_arrives(tmp_path):
    me, client, w, revised = _signed_then_revised_with_late_referee(tmp_path)
    assert w.step()["action"] == "pending"
    rid = json.loads(client.said[-1])["request_id"]
    client.deliver(me, json.loads(client.said[-2])["request_id"])  # recu du retrait, sans effet
    assert w.step()["action"] == "pending"
    client.deliver(me, rid)
    out = w.step()
    assert out["action"] == "signed" and w.signed_members == revised and len(client.said) == 3


def test_pending_list_is_abandoned_only_on_a_definitive_rejection(tmp_path):
    me, client, w, revised = _signed_then_revised_with_late_referee(tmp_path)
    assert w.step()["action"] == "pending"
    client.deliver(me, json.loads(client.said[-1])["request_id"], "rejected", "roster: member already frozen")
    assert w.step()["action"] == "sign-failed"
    assert w.step()["action"] == "wait" and len(client.said) == 3


def test_resume_from_hold_file_restores_the_signed_list_so_a_revision_still_releases_first(tmp_path):
    """Au redemarrage du service, la liste deja signee est relue depuis le fichier de pause : une revision du lead
    declenche bien le retrait avant la nouvelle signature (sinon l'arbitre repondrait 'withdraw before changing')."""
    ref, me, lead, other, client = _setup(tmp_path)
    members = [lead.did, me.did] + ["did:key:z6Mk" + c * 44 for c in "ab"]
    hold = tmp_path / "hold.json"
    hold.write_text(json.dumps({"game": "h5", "members": members, "at": 1.0}), "utf-8")
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id=None,
                                  lead_dids={lead.did}, discovery_room=DISC, dry_run=False, hold_path=hold,
                                  release_game="rimbaud-gang", sleep=lambda s: None, now=iter(range(0, 10000)).__next__)
    assert w.resume() == ("h5", members)
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, members, "l-repost"))
    assert w.step()["action"] == "already-signed"
    revised = [lead.did, me.did, other.did, members[3]]
    client.add(lead, team.roster_message("sonnet-2", "h5", "d-sonnet-2-team-h5", 1, revised, "l-2"))
    w.step()
    posted = [(json.loads(t)["type"], json.loads(t)["game_id"]) for t in client.said]
    assert posted == [("sonnet.withdraw.v1", "h5"), ("sonnet.roster.v1", "h5")]


def test_resume_ignores_a_missing_or_own_team_hold_file(tmp_path):
    ref, me, lead, other, client = _setup(tmp_path)
    hold = tmp_path / "hold.json"
    w = countersign.CounterSigner(client, me, referee_did=ref.did, contest_id="sonnet-2", game_id=None,
                                  lead_dids={lead.did}, discovery_room=DISC, dry_run=False, hold_path=hold,
                                  release_game="rimbaud-gang", sleep=lambda s: None, now=iter(range(0, 10000)).__next__)
    assert w.resume() is None
    hold.write_text(json.dumps({"game": "rimbaud-gang", "members": [me.did], "at": 1.0}), "utf-8")
    assert w.resume() is None and w.signed_game is None
