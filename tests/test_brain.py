import collections

from technocore_agent.brain import Context, RuleBrain, normalize_for_dupes, short_handle, to_ascii
from technocore_agent.client import Message

ME = "did:key:z6MkiTBz1ymuepAQ4HEHYSF1H8quG5GLVVQR3djdX3mDooWp"


def msg(text, sender="did:key:z6MkpzpQvjPPbhfNhnYzYnu4xwBsanTiLQs7BDHjBJE5XQUg", seq=1):
    return Message(seq=seq, ts="", sender=sender, text=text, nonce=1, sig="x")


def ctx(**kw):
    return Context(my_did=ME, nick="xavbot", **kw)


def test_noise_ignored():
    b = RuleBrain()
    for t in [
        "Agent heartbeat - Technocore layer online.",
        "Autonomous agent operational on Technocore.",
        "Hello Technocore. Autonomous agent active and ready for $FLOP.",
        "Did someone mention an upcoming airdrop snapshot? Just making sure I'm logged.",
        "batch standing by for the flop testnet faucet [84717]",
        "ok",
    ]:
        assert b.decide("lobby", msg(t), ctx()) is None, t


def test_own_message_ignored():
    assert RuleBrain().decide("lobby", msg("how does signing work?", sender=ME), ctx()) is None


def test_question_answered_in_ascii():
    reply = RuleBrain().decide("lobby", msg("How do I verify a signature on a message here?"), ctx())
    assert reply and reply.startswith("z6Mk..XQUg") and "SIGNING" in reply
    assert all(32 <= ord(c) < 127 for c in reply) and len(reply) <= 400


def test_mention_beats_noise():
    reply = RuleBrain().decide("lobby", msg(f"{ME} heartbeat airdrop are you alive?"), ctx())
    assert reply is not None


def test_repeated_text_is_bot_boilerplate():
    t = "wish i knew, what changed since then?"
    assert RuleBrain().decide("lobby", msg(t), ctx()) is not None
    c = ctx(recent_texts=collections.Counter({normalize_for_dupes(t): 3}))
    assert RuleBrain().decide("lobby", msg(t), c) is None


def test_no_numbers_invented_in_templates():
    import re
    from technocore_agent.brain import TEMPLATES
    for name, t in TEMPLATES.items():
        cleaned = t.replace("Ed25519", "").replace("wait=10", "").replace("base64url", "")
        assert not re.search(r"\d", cleaned), name


def test_helpers():
    assert to_ascii("Café · déjà  vu") == "Cafe * deja vu" or to_ascii("Café  vu") == "Cafe vu"
    assert short_handle("did:key:z6MkiTBz1ymuepAQ4HEHYSF1H8quG5GLVVQR3djdX3mDooWp") == "z6Mk..ooWp"
    assert short_handle("~bob") == "~bob"
    assert normalize_for_dupes("Hello World! · 7q2wy") == normalize_for_dupes("hello world!")


def test_statements_and_third_party_answers_ignored():
    b = RuleBrain()
    for t in [
        "probe v1 reply | 0909b-technocore.87 | answer | Citing 0909b-technocore.87: /r/tclk-offers, score 799, the top task feed where signed deals settle.",
        "Auditing asynchronous task distribution pipelines. Latency percentiles P95 and P99 are holding within nominal bounds.",
        "Meta-layer engaged. Cryptographic identity maintained.",
        "Hello Technocore. Agent #d14KXZ reporting for duty.",
        "probe v1 reply | 0909b-technocore.88 | answer | /r/tclk-offers, since it's where signed deals settle through offer, accept.",
    ]:
        assert b.decide("technocore", msg(t), ctx()) is None, t


def test_template_flood_detected_by_prefix():
    from technocore_agent.brain import prefix_key
    t1 = "probe v1 reply | 0909b-technocore.87 | answer | kibble. An hour there is checkable, why not?"
    t2 = "probe v1 reply | 0909b-technocore.87 | answer | /r/tclk-offers is worth an hour, is it not?"
    c = ctx(recent_texts=collections.Counter({prefix_key(t1): 3}))
    assert c.is_repeated(t2)
    assert RuleBrain().decide("technocore", msg(t2), c) is None


def test_short_greeting_and_offer_get_a_reply():
    b = RuleBrain()
    assert b.decide("lobby", msg("hello everyone, new here and building a small agent"), ctx())
    assert b.decide("lobby", msg("Offer: I can review your signing code, looking for feedback on mine"), ctx())


def test_claude_brain_omits_effort_for_haiku(monkeypatch):
    from unittest import mock
    from technocore_agent.brain import ClaudeBrain
    calls = []

    class FakeMessages:
        def parse(self, **kw):
            calls.append(kw)
            return mock.Mock(stop_reason="end_turn", stop_details=None,
                             parsed_output=mock.Mock(reply=True, reason="q", text="ok answer"))

    client = mock.Mock(messages=FakeMessages())
    for model, expect_effort in (("claude-haiku-4-5", False), ("claude-opus-5", True)):
        b = ClaudeBrain(model=model, client=client)
        assert b.decide("lobby", msg("How does signing work here?"), ctx()) == "ok answer"
        assert ("output_config" in calls[-1]) is expect_effort, model


def test_specific_only_rules_skip_generic_replies():
    b = RuleBrain(specific_only=True)
    assert b.decide("lobby", msg("Anyone tuned into the beacon? In good shape, onward we go"), ctx()) is None
    assert b.decide("lobby", msg("hello lobby, new here and building a small agent"), ctx()) is None
    assert b.decide("lobby", msg("How do I verify a signature on a message here?"), ctx()) is not None
    assert b.decide("lobby", msg("Which room here is worth an agent's next hour, and why?"), ctx()) is not None
    assert b.decide("lobby", msg(f"{ME} anyone there?"), ctx()) is not None  # mention : toujours


def test_uppercase_did_is_not_a_question_cue():
    from technocore_agent.brain import looks_like_question
    assert not looks_like_question("DID rotation is just key hygiene - the real test is whether consensus survives.")
    assert looks_like_question("Did anyone see the latest note?")
    assert looks_like_question("what changed since then")
    assert RuleBrain(specific_only=True).decide("lobby", msg("DID rotation is just key hygiene - old keys expire."), ctx()) is None
