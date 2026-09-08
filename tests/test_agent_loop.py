from unittest import mock

from technocore_agent import identity
from technocore_agent.agent import Agent
from technocore_agent.brain import Brain
from technocore_agent.client import Message, RoomPage
from technocore_agent.config import Config
from technocore_agent.state import State


class RecordingBrain(Brain):
    name = "rec"

    def __init__(self):
        self.calls = []

    def decide_batch(self, room, candidates, ctx, max_replies):
        self.calls.append([m.seq for m in candidates])
        return {}


def drain(agent):
    """Attend la fin des consultations en arriere-plan (tests)."""
    import concurrent.futures
    concurrent.futures.wait([f for f, _ in agent.inflight.values()], timeout=5)


def make_agent(tmp_path, think_seconds=30.0):
    cfg = Config(home=tmp_path, rooms=["r"], nick="x", poll_seconds=1, note_extra="",
                 max_replies_per_room_per_hour=20, max_replies_per_hour=45, sender_cooldown_seconds=600,
                 model=None, base_url="http://x", think_seconds=think_seconds)
    ident = identity.create(tmp_path / "k.pem", "s")
    client = mock.Mock()
    # les tests de boucle ne testent pas l'envoi : un 422 simule evite toute confirmation
    from technocore_agent.client import Duplicate
    client.say_signed.side_effect = Duplicate("422 dup", "u")
    brain = RecordingBrain()
    agent = Agent(cfg, ident, client, brain, State.load(cfg.state_path))
    agent.state.cursors["r"] = 0
    return agent, client, brain


def page(seqs, texts):
    msgs = [Message(seq=s, ts="", sender=f"did:key:z6Mk{s}", text=t, nonce=1, sig="x") for s, t in zip(seqs, texts)]
    return RoomPage(room="r", first_seq=seqs[0], last_seq=seqs[-1], messages=msgs)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_statements_accumulate_until_think_interval(tmp_path):
    agent, client, brain = make_agent(tmp_path)
    clock = Clock(1000.0)
    with mock.patch("technocore_agent.agent.time.time", clock):
        client.read.return_value = page([1, 2], ["Calm evening on the network, all steady here tonight.", "Agent heartbeat online."])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == [[1]]  # premier appel (last_think=0)
        clock.t = 1010.0
        client.read.return_value = page([3], ["Nothing new to report from this node this evening."])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == [[1]]  # declaration, 10 s plus tard : accumule, pas d'appel
        clock.t = 1040.0
        client.read.return_value = page([4], ["Still quiet here, the relay is doing its job as usual."])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == [[1]]  # 40 s : declarations seules, on attend 60 s
        clock.t = 1065.0
        client.read.return_value = page([5], ["Same picture on my side, nothing worth flagging tonight."])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == [[1], [3, 4, 5]]  # 65 s : un seul appel avec tout ce qui attendait
    assert agent.state.cursors["r"] == 5


def test_mention_triggers_immediately(tmp_path):
    agent, client, brain = make_agent(tmp_path)
    clock = Clock(1000.0)
    with mock.patch("technocore_agent.agent.time.time", clock):
        client.read.return_value = page([1], ["How do I sign a message here?"])
        agent.process_room("r")
        clock.t = 1005.0
        client.read.return_value = page([2], [f"{agent.ident.did} are you there?"])
        drain(agent)
        agent.process_room("r")
        drain(agent)
    assert brain.calls == [[1], [2]]
