"""Gestionnaire de roster : remplace les sieges qui ne signent pas, en gardant les 26 lettres couvertes."""
import json

from technocore_agent import identity
from technocore_agent.client import Message, RoomPage, SayResult
from technocore_agent.sonnet import manager, team

DISC = "mb-sonnet-2-discovery"
GAME = "rg"
ROOM = "d-sonnet-2-team-rg"
# lexique minimal : les mots-cles doivent rester jouables par l'union des alphabets
KEY = {"the": 1, "and": 1, "of": 1, "love": 1}


class World:
    def __init__(self, tmp_path):
        self.ref = identity.create(tmp_path / "ref.pem", "pw")
        self.me = identity.create(tmp_path / "me.pem", "pw")
        self.ids = {}
        self.seq = 0
        self.msgs = []
        self.t = 0  # secondes simulees ; ts ISO derive

    def person(self, name):
        if name not in self.ids:
            self.ids[name] = identity.create(f"/tmp/never/{name}.pem".replace("/tmp/never", str(self._base)), "pw") \
                if False else identity.create(self._base / f"{name}.pem", "pw")
        return self.ids[name]

    def epoch(self, sec):
        return manager.parse_ts(f"2026-09-13T{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}Z")

    def post(self, ident, text, at=None):
        self.seq += 1
        if at is not None:
            self.t = at
        ts = f"2026-09-13T{self.t // 3600:02d}:{(self.t % 3600) // 60:02d}:{self.t % 60:02d}Z"
        self.msgs.append(Message(self.seq, ts, ident.did, text, self.seq, ident.sign(DISC, self.seq, text)))
        return self.seq

    def receipt(self, rid, sender, status="accepted", reason="", **extra):
        body = {"type": "sonnet.receipt.v1", "status": status, "reason": reason, "request_id": rid,
                "sender_did": sender, "contest_id": "sonnet-2", **extra}
        return self.post(self.ref, json.dumps(body, separators=(",", ":")))

    def roster(self, ident, members, rid, at=None):
        return self.post(ident, team.roster_message("sonnet-2", GAME, ROOM, 1, members, rid), at)


def _world(tmp_path):
    w = World(tmp_path)
    w._base = tmp_path
    return w


def test_analysis_flags_signers_free_writers_leads_and_activity(tmp_path):
    w = _world(tmp_path)
    a, b, lead = w.person("a"), w.person("b"), w.person("lead")
    w.post(a, json.dumps({"type": "sonnet.application.v1", "contest_id": "sonnet-2", "game_id": "other", "text": "yes"}), at=100)
    w.roster(b, [b.did, a.did, lead.did, w.me.did], "b-1", at=200)
    w.receipt("b-1", b.did)
    w.post(b, team.withdraw_message("sonnet-2", GAME, "b-wd"), at=300)
    w.receipt("b-wd", b.did)
    w.post(lead, team.team_request_message("sonnet-2", "x", "r"), at=400)
    an = manager.analyse(w.msgs, DISC, w.ref.did)
    assert an[a.did].signed_any is False and an[a.did].last_activity == w.epoch(100)
    assert an[b.did].signed_any is True and an[b.did].free_by_withdraw is True
    assert an[lead.did].is_lead is True
    assert an[b.did].applied_to == set() and an[a.did].applied_to == {"other"}


def test_pick_candidates_keeps_coverage_and_prefers_applicants(tmp_path):
    w = _world(tmp_path)
    good, other, lead, stale = w.person("good"), w.person("other"), w.person("lead"), w.person("stale")
    w.post(other, json.dumps({"type": "sonnet.application.v1", "game_id": "elsewhere", "text": "yes"}), at=1000)
    w.post(good, json.dumps({"type": "sonnet.application.v1", "game_id": GAME, "text": "yes-rg"}), at=1100)
    w.post(lead, team.team_request_message("sonnet-2", "x", "r"), at=1200)
    w.post(stale, json.dumps({"type": "sonnet.application.v1", "game_id": GAME, "text": "yes-rg"}), at=10)
    an = manager.analyse(w.msgs, DISC, w.ref.did)
    rules = manager.Rules(patience_s=1800, active_window_s=1000)  # stale (t=10) est hors fenetre a t=1500
    cands = manager.pick_candidates(an, me=w.me.did, members=[w.me.did], game_id=GAME, key_words=KEY,
                                    now=w.epoch(1500), rules=rules, coverage_required=False)
    assert cands[0] == good.did and other.did in cands
    assert lead.did not in cands and stale.did not in cands


def test_decide_waits_for_patience_then_replaces_oldest_pending(tmp_path):
    w = _world(tmp_path)
    p1, p2, cand = w.person("p1"), w.person("p2"), w.person("cand")
    members = [w.me.did, p1.did, p2.did, w.person("p3").did]
    w.roster(w.me, members, "me-1", at=1000)
    w.receipt("me-1", w.me.did, roster_ready=False)
    w.post(cand, json.dumps({"type": "sonnet.application.v1", "game_id": GAME, "text": "yes-rg"}), at=1200)
    st = {"members": members, "signed": [w.me.did], "pending": [p1.did, p2.did, members[3]], "ready": False, "roster_seq": 1}
    an = manager.analyse(w.msgs, DISC, w.ref.did)
    rules = manager.Rules(patience_s=1800, active_window_s=7200)
    assert manager.decide(st, an, me=w.me.did, game_id=GAME, key_words=KEY, now=w.epoch(2000), rules=rules,
                          posted_at=w.epoch(1000), coverage_required=False) is None
    act = manager.decide(st, an, me=w.me.did, game_id=GAME, key_words=KEY, now=w.epoch(3000), rules=rules,
                         posted_at=w.epoch(1000), coverage_required=False)
    assert act is not None and act.old == p1.did and act.new == cand.did
    assert act.members == [w.me.did, cand.did, p2.did, members[3]]


def test_decide_replaces_immediately_a_member_locked_by_prior_consent(tmp_path):
    w = _world(tmp_path)
    locked, cand = w.person("locked"), w.person("cand")
    members = [w.me.did, locked.did, w.person("x").did, w.person("y").did]
    w.roster(w.me, members, "me-1", at=1000)
    w.receipt("me-1", w.me.did, roster_ready=False)
    w.roster(locked, members, "l-1", at=1010)
    w.receipt("l-1", locked.did, status="rejected", reason="consent: withdraw before changing")
    w.post(cand, json.dumps({"type": "sonnet.application.v1", "game_id": GAME, "text": "yes-rg"}), at=1100)
    st = {"members": members, "signed": [w.me.did], "pending": members[1:], "ready": False, "roster_seq": 1}
    an = manager.analyse(w.msgs, DISC, w.ref.did)
    act = manager.decide(st, an, me=w.me.did, game_id=GAME, key_words=KEY, now=w.epoch(1200),
                         rules=manager.Rules(patience_s=1800, active_window_s=7200), posted_at=w.epoch(1000),
                         coverage_required=False)
    assert act is not None and act.old == locked.did and act.new == cand.did


def test_decide_does_nothing_when_ready_or_without_candidate(tmp_path):
    w = _world(tmp_path)
    members = [w.me.did] + [w.person(n).did for n in "abc"]
    an = manager.analyse(w.msgs, DISC, w.ref.did)
    rules = manager.Rules(patience_s=10, active_window_s=7200)
    ready = {"members": members, "signed": members, "pending": [], "ready": True, "roster_seq": 1}
    assert manager.decide(ready, an, me=w.me.did, game_id=GAME, key_words=KEY, now=w.epoch(5000), rules=rules,
                          posted_at=0, coverage_required=False) is None
    st = {"members": members, "signed": [w.me.did], "pending": members[1:], "ready": False, "roster_seq": 1}
    assert manager.decide(st, an, me=w.me.did, game_id=GAME, key_words=KEY, now=w.epoch(5000), rules=rules,
                          posted_at=0, coverage_required=False) is None


class FakeClient:
    def __init__(self, world):
        self.w = world
        self.said = []
        self.receipts_for = {}

    def export(self, room):
        return list(self.w.msgs), 1

    def read(self, room, since=None, limit=200, wait=None):
        new = [m for m in self.w.msgs if since is None or m.seq > since]
        return RoomPage(room, new[0].seq if new else None, new[-1].seq if new else None, new, generation=1)

    def say_signed(self, ident, room, text, nonce):
        self.said.append((room, text))
        seq = self.w.post(ident, text)
        data = json.loads(text) if text.startswith("{") else None
        if data and data.get("type") in ("sonnet.withdraw.v1", "sonnet.roster.v1"):
            self.w.receipt(data["request_id"], ident.did, roster_ready=False)
        return SayResult("", seq, True)


def test_live_replacement_withdraws_waits_signs_and_invites(tmp_path):
    w = _world(tmp_path)
    p1, cand = w.person("p1"), w.person("cand")
    members = [w.me.did, p1.did, w.person("x").did, w.person("y").did]
    w.roster(w.me, members, "xav-rg-roster-1", at=1000)
    w.receipt("xav-rg-roster-1", w.me.did, roster_ready=False)
    w.post(cand, json.dumps({"type": "sonnet.application.v1", "game_id": GAME, "text": "yes-rg"}), at=1100)
    client = FakeClient(w)
    m = manager.RosterManager(client, w.me, referee_did=w.ref.did, contest_id="sonnet-2", game_id=GAME,
                              discovery_room=DISC, poem_room=ROOM, generation=1, key_words=KEY,
                              state_path=tmp_path / "m.json", rules=manager.Rules(patience_s=100, active_window_s=7200),
                              dry_run=False, coverage_required=False, now=lambda: w.epoch(5000), sleep=lambda s: None)
    outcome = m.run_once()
    types = [json.loads(t)["type"] if t.startswith("{") else "note" for _, t in client.said]
    assert types == ["sonnet.withdraw.v1", "sonnet.roster.v1", "note"]
    assert json.loads(client.said[1][1])["members"] == [w.me.did, cand.did, members[2], members[3]]
    assert cand.did[-8:] in client.said[2][1] and "withdraw" not in client.said[2][1]
    assert outcome["action"] == "replaced" and json.loads((tmp_path / "m.json").read_text())["replacements"] == 1


def test_dry_run_plans_but_never_writes(tmp_path):
    w = _world(tmp_path)
    p1, cand = w.person("p1"), w.person("cand")
    members = [w.me.did, p1.did, w.person("x").did, w.person("y").did]
    w.roster(w.me, members, "xav-rg-roster-1", at=1000)
    w.receipt("xav-rg-roster-1", w.me.did, roster_ready=False)
    w.post(cand, json.dumps({"type": "sonnet.application.v1", "game_id": GAME, "text": "yes-rg"}), at=1100)
    client = FakeClient(w)
    m = manager.RosterManager(client, w.me, referee_did=w.ref.did, contest_id="sonnet-2", game_id=GAME,
                              discovery_room=DISC, poem_room=ROOM, generation=1, key_words=KEY,
                              state_path=tmp_path / "m.json", rules=manager.Rules(patience_s=100, active_window_s=7200),
                              dry_run=True, coverage_required=False, now=lambda: w.epoch(5000), sleep=lambda s: None)
    outcome = m.run_once()
    assert client.said == [] and outcome["action"] == "planned" and outcome["new"] == cand.did
