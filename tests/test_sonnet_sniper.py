"""Sniper : candidater UNE fois aux equipes reelles (>= 2 consentements acceptes) qui ont un siege muet,
et n'accepter en contre-signature que les rosters postes par un signataire accepte de cette equipe."""
import json

from technocore_agent import identity
from technocore_agent.client import Message, RoomPage, SayResult
from technocore_agent.sonnet import sniper, team

DISC = "mb-sonnet-2-discovery"


class World:
    def __init__(self, tmp_path):
        self.base = tmp_path
        self.ref = identity.create(tmp_path / "ref.pem", "pw")
        self.me = identity.create(tmp_path / "me.pem", "pw")
        self.ids = {}
        self.msgs = []
        self.seq = 0
        self.t = 0

    def person(self, n):
        if n not in self.ids:
            self.ids[n] = identity.create(self.base / f"{n}.pem", "pw")
        return self.ids[n]

    def ts(self, sec):
        return f"2026-09-14T{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}Z"

    def epoch(self, sec):
        return sniper.parse_ts(self.ts(sec))

    def post(self, ident, text, at=None):
        if at is not None:
            self.t = at
        self.seq += 1
        self.msgs.append(Message(self.seq, self.ts(self.t), ident.did, text, self.seq, ident.sign(DISC, self.seq, text)))
        return self.seq

    def receipt(self, rid, sender, ready=False):
        return self.post(self.ref, json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": rid,
                                               "sender_did": sender, "roster_ready": ready, "contest_id": "sonnet-2"}))


def _team(w, game="tm", n_signed=3, at=1000):
    lead, b, c, d = (w.person(f"{game}-{x}") for x in "lbcd")
    members = [lead.did, b.did, c.did, d.did]
    for i, p in enumerate([lead, b, c, d][:n_signed]):
        w.post(p, team.roster_message("sonnet-2", game, f"d-sonnet-2-team-{game}", 1, members, f"{game}-r-{i}"), at=at + i)
        w.receipt(f"{game}-r-{i}", p.did)
    return lead, members


def test_team_cores_counts_accepted_signers_on_the_latest_list(tmp_path):
    w = World(tmp_path)
    lead, members = _team(w, "tm", n_signed=3)
    cores = sniper.team_cores(w.msgs, DISC, w.ref.did)
    core = cores["tm"]
    assert core["latest"] == members and len(core["signed"]) == 3 and core["ready"] is False
    assert core["pending"] == [members[3]] and core["lead"] == lead.did


def test_openings_need_a_real_core_and_a_silent_seat(tmp_path):
    w = World(tmp_path)
    _team(w, "real", n_signed=3, at=1000)     # siege 4 muet
    _team(w, "weak", n_signed=1, at=1000)     # pas assez de consentements
    lead3, members3 = _team(w, "busy", n_signed=3, at=1000)
    w.post(w.person("busy-d"), json.dumps({"type": "sonnet.note.v1", "text": "signing soon"}), at=2900)  # siege actif
    cores = sniper.team_cores(w.msgs, DISC, w.ref.did)
    ops = sniper.find_openings(cores, w.msgs, now=w.epoch(3000), min_core=2, silent_s=1800, me=w.me.did)
    assert [o["game"] for o in ops] == ["real"]


class FakeClient:
    def __init__(self, w):
        self.w = w
        self.said = []

    def export(self, room):
        return list(self.w.msgs), 1

    def read(self, room, since=None, limit=200, wait=None):
        new = [m for m in self.w.msgs if m.seq > (since or 0)]
        return RoomPage(room, new[0].seq if new else None, new[-1].seq if new else None, new, generation=1)

    def say_signed(self, ident, room, text, nonce):
        self.said.append(text)
        return SayResult("", self.w.post(ident, text), True)


def test_sniper_applies_once_per_list_and_respects_hourly_cap(tmp_path):
    w = World(tmp_path)
    _team(w, "one", n_signed=3, at=1000)
    _team(w, "two", n_signed=2, at=1000)
    _team(w, "three", n_signed=3, at=1000)
    client = FakeClient(w)
    clock = [w.epoch(3000)]
    s = sniper.Sniper(client, w.me, referee_did=w.ref.did, contest_id="sonnet-2", discovery_room=DISC,
                      state_path=tmp_path / "s.json", dry_run=False, registration_seq=127300,
                      letters="abcdefhikmnopsuwxyz", max_per_hour=2, now=lambda: clock[0])
    out = s.run_once()
    apps = [json.loads(t) for t in client.said]
    assert len(apps) == 2 and all(a["type"] == "sonnet.application.v1" and a["no_live_roster_consent"] is True for a in apps)
    assert out["applied"] == 2
    assert s.run_once()["applied"] == 0  # plafond horaire atteint et listes deja candidatees
    clock[0] += 3700
    assert s.run_once()["applied"] == 1  # la troisieme, une seule fois


def test_countersign_policy_accepts_only_rosters_posted_by_an_accepted_core_signer(tmp_path):
    w = World(tmp_path)
    lead, members = _team(w, "tm", n_signed=3, at=1000)
    cores = sniper.team_cores(w.msgs, DISC, w.ref.did)
    policy = sniper.core_policy(cores, min_core=2)
    revised = members[:3] + [w.me.did]
    ok_msg = Message(99, w.ts(4000), lead.did, team.roster_message("sonnet-2", "tm", "d-sonnet-2-team-tm", 1, revised, "x"), 1, "sig")
    stranger = w.person("stranger")
    bad_msg = Message(100, w.ts(4000), stranger.did, team.roster_message("sonnet-2", "tm", "d-sonnet-2-team-tm", 1, revised, "y"), 1, "sig")
    assert policy(ok_msg, json.loads(ok_msg.text)) is True
    assert policy(bad_msg, json.loads(bad_msg.text)) is False
    other_game = Message(101, w.ts(4000), lead.did, team.roster_message("sonnet-2", "new", "d-sonnet-2-team-new", 1, revised, "z"), 1, "sig")
    assert policy(other_game, json.loads(other_game.text)) is False
