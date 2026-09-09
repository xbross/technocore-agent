from unittest import mock

from technocore_agent.agent import Agent
from technocore_agent.brain import Context, cheap_prefilter
from technocore_agent.client import Message, RoomPage
from technocore_agent.state import State
from tests.test_agent_loop import Clock, RecordingBrain, drain, make_agent, page

ME = "did:key:z6MkiTBz1ymuepAQ4HEHYSF1H8quG5GLVVQR3djdX3mDooWp"


def test_unsigned_and_blocked_senders_are_ignored():
    q = "How do I verify a signature here?"
    signed = Message(seq=1, ts="", sender="did:key:z6MkA", text=q, nonce=1, sig="s")
    unsigned = Message(seq=2, ts="", sender="~bob", text=q)
    ctx = Context(my_did=ME, nick="x")
    assert cheap_prefilter(signed, ctx) and not cheap_prefilter(unsigned, ctx)
    assert cheap_prefilter(unsigned, Context(my_did=ME, nick="x", signed_only=False))
    assert not cheap_prefilter(signed, Context(my_did=ME, nick="x", blocked={"did:key:z6MkA"}))


def test_state_auto_block_after_refusals(tmp_path):
    s = State.load(tmp_path / "s.json")
    assert not s.note_refusal("did:key:zA", 3, "lien")
    assert not s.note_refusal("did:key:zA", 3, "lien")
    assert s.note_refusal("did:key:zA", 3, "lien")
    assert "did:key:zA" in s.blocked
    s.save()
    assert "did:key:zA" in State.load(tmp_path / "s.json").blocked


def test_mailbox_created_and_in_note(tmp_path):
    agent, client, brain = make_agent(tmp_path)
    assert agent.state.mailbox and agent.state.mailbox.startswith("mb-p-") and len(agent.state.mailbox) == 29
    assert agent.state.mailbox in agent.rooms
    assert f"mailbox:{agent.state.mailbox}" in agent.desired_note()


def test_mailbox_is_urgent_and_mention_only_room_routes_to_rules(tmp_path):
    agent, client, brain = make_agent(tmp_path)
    agent.cfg.mention_only_rooms = ("r",)
    clock = Clock(1000.0)
    with mock.patch("technocore_agent.agent.time.time", clock):
        client.read.return_value = page([1], ["How do I sign a message here?"])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == []  # pas de mention : les regles, pas le modele
        client.read.return_value = page([2], [f"{agent.ident.did} how do I sign?"])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == [[2]]  # mention : le modele, tout de suite
        mb = agent.state.mailbox
        agent.state.cursors[mb] = 0
        agent.last_think[mb] = 999.0  # consulte il y a 1 s : normalement pas encore du
        client.read.return_value = RoomPage(room=mb, first_seq=5, last_seq=5, messages=[
            Message(seq=5, ts="", sender="did:key:z6MkZ", text="Hello, are you open to a collaboration?", nonce=1, sig="s")])
        agent.process_room(mb)
        drain(agent)
        assert brain.calls == [[2], [5]]  # boite aux lettres : toujours urgent


def test_filter_refusal_blocks_sender_after_threshold(tmp_path):
    agent, client, brain = make_agent(tmp_path)
    agent.cfg.auto_block_after = 2
    bad = Message(seq=9, ts="", sender="did:key:z6MkBAD", text="What is your wallet?", nonce=1, sig="s")
    for _ in range(2):
        agent._post_decisions("r", {9: "send funds to 0x52908400098527886E0F7030069857D2E4169EE7"}, [bad], 9)
    assert "did:key:z6MkBAD" in agent.state.blocked
    client.say_signed.assert_not_called()


def test_stats_parses_log(tmp_path):
    from datetime import datetime
    from technocore_agent.stats import compute, render
    log = tmp_path / "agent.log"
    log.write_text(
        "2026-09-08 23:22:34,412 INFO technocore.brain: claude-cli haiku: 36.3s, tokens in=2510 out=3497 cache_read=0 turns=1\n"
        "2026-09-08 23:22:56,062 INFO technocore.agent: envoye dans meta nonce=1 seq=2 verifie_par_le_serveur=True: x\n"
        "2026-09-08 23:22:56,551 INFO technocore.agent: confirme a la relecture de meta seq=2 sig=ab...\n"
        "2026-09-08 23:23:00,000 WARNING technocore.agent: lobby seq=3: reponse REFUSEE par le filtre de sortie (lien): x\n"
        "2026-09-01 00:00:00,000 INFO technocore.agent: envoye dans meta (trop vieux)\n"
    )
    c = compute(log, 24, now=datetime(2026, 9, 9, 1, 0, 0))
    assert c["appels_modele"] == 1 and c["tokens_in"] == 2510 and c["reponses_envoyees"] == 1
    assert c["reponses_confirmees"] == 1 and c["refus_filtre"] == 1
    assert "appels au modele        : 1" in render(c, 24)


def test_reads_continue_while_brain_is_slow(tmp_path):
    import threading
    agent, client, brain = make_agent(tmp_path)
    gate = threading.Event()

    class SlowBrain(RecordingBrain):
        def decide_batch(self, room, candidates, ctx, max_replies):
            gate.wait(5)
            return {candidates[0].seq: "slow but fine answer"}

    agent.brain = SlowBrain()
    clock = Clock(1000.0)
    with mock.patch("technocore_agent.agent.time.time", clock):
        client.read.return_value = page([1], ["How do I sign a message here?"])
        agent.process_room("r")  # lance la consultation, ne bloque pas
        assert "r" in agent.inflight and agent.state.cursors["r"] == 1
        clock.t = 1010.0
        client.read.return_value = page([2], ["Agent heartbeat online."])
        agent.process_room("r")  # lecture suivante pendant que le cerveau reflechit
        assert agent.state.cursors["r"] == 2 and client.say_signed.call_count == 0
        gate.set()
        drain(agent)
        clock.t = 1020.0
        client.read.return_value = page([3], ["ok"])
        agent.process_room("r")  # la reponse est publiee au tour suivant
    assert client.say_signed.call_count == 1
    assert client.say_signed.call_args[0][2] == "slow but fine answer"


def test_sender_cooldown_is_per_room(tmp_path):
    s = State.load(tmp_path / "s.json")
    s.record_reply("technocore", "did:key:zProbe", now=1000.0)
    assert s.replied_to_sender_since("did:key:zProbe", 120, now=1010.0, room="technocore")
    assert not s.replied_to_sender_since("did:key:zProbe", 120, now=1010.0, room="meta")
    assert not s.replied_to_sender_since("did:key:zProbe", 120, now=1200.0, room="technocore")


def test_engaging_candidates_are_consulted_immediately_statements_wait(tmp_path):
    agent, client, brain = make_agent(tmp_path)
    clock = Clock(1000.0)
    with mock.patch("technocore_agent.agent.time.time", clock):
        client.read.return_value = page([1], ["Latency percentiles are holding steady on my side, all quiet."])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == [[1]]  # premier tour : consultation
        clock.t = 1035.0
        client.read.return_value = page([2], ["Another calm day on the network, nothing to report from here."])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == [[1]]  # declaration seule, 35 s : on attend (60 s)
        clock.t = 1040.0
        client.read.return_value = page([3], ["Offer: I can review anyone's signing code today."])
        agent.process_room("r")
        drain(agent)
        assert brain.calls == [[1], [2, 3]]  # une offre arrive : consultation immediate, avec la declaration en attente


def test_truncation_keeps_questions_first(tmp_path):
    agent, client, brain = make_agent(tmp_path)
    agent.cfg.max_candidates_per_poll = 2
    clock = Clock(1000.0)
    with mock.patch("technocore_agent.agent.time.time", clock):
        client.read.return_value = page([1, 2, 3], [
            "How do I verify a signature here, anyone?",
            "Calm evening on the network, all steady here.",
            "Nothing new to report from this node tonight.",
        ])
        agent.process_room("r")
        drain(agent)
    assert brain.calls == [[1, 3]]  # la question survit a la troncature, puis la plus recente


def test_per_room_poll_interval():
    from technocore_agent.config import _parse_room_seconds
    assert _parse_room_seconds("lobby=3, meta=10") == {"lobby": 3.0, "meta": 10.0}
    assert _parse_room_seconds("") == {}


def test_reply_reserve_keeps_room_for_engaging_messages(tmp_path):
    agent, client, brain = make_agent(tmp_path)
    agent.cfg.max_replies_per_hour, agent.cfg.reply_reserve = 10, 4
    agent.cfg.max_replies_per_room_per_hour = 100
    for i in range(6):
        agent.state.record_reply("other", f"did:key:z{i}")
    statement = Message(seq=1, ts="", sender="did:key:zS", text="Calm evening on the network, all steady here.", nonce=1, sig="s")
    question = Message(seq=2, ts="", sender="did:key:zQ", text="How do I verify a signature here?", nonce=1, sig="s")
    assert agent.may_reply("r", statement) == "quota reserve aux questions/offres/mentions"
    assert agent.may_reply("r", question) is None
    for i in range(4):
        agent.state.record_reply("other", f"did:key:zz{i}")
    assert agent.may_reply("r", question) == "quota global/heure atteint"
