import json
import os
import stat

from technocore_agent.brain import ClaudeCliBrain, Context, RuleBrain
from technocore_agent.client import Message

ME = "did:key:z6MkiTBz1ymuepAQ4HEHYSF1H8quG5GLVVQR3djdX3mDooWp"


def fake_claude(tmp_path, body: str, exit_code: int = 0):
    """Ecrit un faux binaire `claude` qui imprime `body` et sort avec `exit_code`."""
    path = tmp_path / "claude"
    path.write_text(f"#!/bin/sh\ncat > /dev/null\nprintf '%s' '{body}'\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def msgs():
    return [
        Message(seq=10, ts="", sender="did:key:z6MkAAAA1111", text="How do I verify a signature here?", nonce=1, sig="x"),
        Message(seq=11, ts="", sender="did:key:z6MkBBBB2222", text="Agent heartbeat - online.", nonce=1, sig="x"),
        Message(seq=12, ts="", sender="did:key:z6MkCCCC3333", text="Anyone know a good room for compute jobs?", nonce=1, sig="x"),
    ]


def ctx():
    return Context(my_did=ME, nick="xav")


def test_batch_parses_structured_output_and_filters(tmp_path):
    result = {"type": "result", "is_error": False, "duration_ms": 1500, "usage": {"input_tokens": 100, "output_tokens": 20},
              "structured_output": {"replies": [
                  {"seq": 10, "text": "z6Mk..1111 sign room|nonce|text with Ed25519, see /llms.txt"},
                  {"seq": 11, "text": "should be dropped: noise line was never a candidate"},
                  {"seq": 12, "text": "café answer"},
              ]}}
    brain = ClaudeCliBrain(binary=fake_claude(tmp_path, json.dumps(result)))
    out = brain.decide_batch("lobby", msgs(), ctx(), max_replies=5)
    assert set(out) == {10, 12}
    assert out[12] == "cafe answer"  # ASCII force


def test_batch_respects_max_replies(tmp_path):
    result = {"is_error": False, "result": json.dumps({"replies": [{"seq": 10, "text": "a"}, {"seq": 12, "text": "b"}]})}
    brain = ClaudeCliBrain(binary=fake_claude(tmp_path, json.dumps(result)))
    assert len(brain.decide_batch("lobby", msgs(), ctx(), max_replies=1)) == 1


def test_fallback_to_rules_when_cli_fails(tmp_path):
    for body, code in (("", 1), ("not json", 0), (json.dumps({"is_error": True, "result": "Not logged in"}), 0)):
        brain = ClaudeCliBrain(binary=fake_claude(tmp_path, body, code), fallback=RuleBrain())
        out = brain.decide_batch("lobby", msgs(), ctx(), max_replies=5)
        assert 10 in out and 11 not in out  # les regles prennent le relais


def test_missing_binary_falls_back():
    brain = ClaudeCliBrain(binary="/nonexistent/claude")
    assert 10 in brain.decide_batch("lobby", msgs(), ctx(), max_replies=5)


def test_cli_invocation_disables_tools(tmp_path):
    seen = tmp_path / "args.txt"
    path = tmp_path / "claude"
    reply = json.dumps({"structured_output": {"replies": []}})
    script = "#!/bin/sh\n" + 'printf "%s\\n" "$@" > ' + str(seen) + "\n" + "printf '%s' '" + reply + "'\n"
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    ClaudeCliBrain(binary=str(path), model="haiku").decide_batch("lobby", msgs(), ctx(), 3)
    args = seen.read_text().splitlines()
    assert args[:4] == ["-p", "--model", "haiku", "--tools"] and args[4] == ""
    assert "--no-session-persistence" in args and "--json-schema" in args


def test_hourly_call_budget_and_failure_pause(tmp_path):
    ok = json.dumps({"structured_output": {"replies": [{"seq": 10, "text": "model answer"}]}})
    brain = ClaudeCliBrain(binary=fake_claude(tmp_path, ok), max_calls_per_hour=2, fallback=RuleBrain())
    assert brain.decide_batch("lobby", msgs(), ctx(), 5)[10] == "model answer"
    assert brain.decide_batch("lobby", msgs(), ctx(), 5)[10] == "model answer"
    third = brain.decide_batch("lobby", msgs(), ctx(), 5)  # plafond atteint -> regles
    assert 10 in third and third[10] != "model answer"
    assert len(brain._calls) == 2

    (tmp_path / "f").mkdir(exist_ok=True)
    failing = ClaudeCliBrain(binary=fake_claude(tmp_path / "f", "", 1), failure_pause=999, fallback=RuleBrain())
    failing.decide_batch("lobby", msgs(), ctx(), 5)
    assert failing._paused_until > 0
    calls_before = len(failing._calls)
    failing.decide_batch("lobby", msgs(), ctx(), 5)  # en pause : aucun nouvel appel
    assert len(failing._calls) == calls_before
