import hashlib
import json

import sonnet_cli

DID = "did:key:z6MknPooKck2cXu52NMM9h3cmeSxBzi2KkZEaHUSk3SwzfDk"


def _setup(tmp_path):
    pkg = tmp_path / "contest" / "package"
    pkg.mkdir(parents=True)
    dic = pkg / "cmudict.dict"
    dic.write_text("moon M UW1 N\nsoon S UW1 N\nthe DH AH0\nand AH0 N D\nocean OW1 SH AH0 N\n", "utf-8")
    val = pkg / "sonnet_validate.py"
    val.write_text("def validate_word(t, d, l):\n    return 1\n", "utf-8")
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    (tmp_path / "sonnet.toml").write_text(f'''
[contest]
id = "sonnet-2"
referee_did = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
[rooms]
rules = "d-sonnet-2-rules"
[package]
dir = "contest/package"
commit = "e199"
[package.sha256]
"cmudict.dict" = "{sha(dic)}"
"sonnet_validate.py" = "{sha(val)}"
[participant]
did = "{DID}"
role = "writer"
''', "utf-8")
    return tmp_path / "sonnet.toml"


def test_alphabet_command_prints_letters_and_missing(tmp_path, capsys):
    cfg = _setup(tmp_path)
    assert sonnet_cli.main(["--config", str(cfg), "alphabet"]) == 0
    out = capsys.readouterr().out
    assert "abcdefhikmnopsuwxyz" in out and "gjlqrtv" in out


def test_words_command_lists_playable_words_with_syllables(tmp_path, capsys):
    cfg = _setup(tmp_path)
    assert sonnet_cli.main(["--config", str(cfg), "words", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["playable"] == {"and": 1, "moon": 1, "ocean": 2, "soon": 1}


def test_words_command_refuses_tampered_package(tmp_path, capsys):
    cfg = _setup(tmp_path)
    (tmp_path / "contest/package/cmudict.dict").write_text("evil X\n", "utf-8")
    assert sonnet_cli.main(["--config", str(cfg), "words"]) != 0
    assert "sha256" in capsys.readouterr().err


def test_roster_command_reports_coverage(tmp_path, capsys):
    cfg = _setup(tmp_path)
    other = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
    assert sonnet_cli.main(["--config", str(cfg), "roster", DID, other]) == 0
    out = capsys.readouterr().out
    assert "missing" in out and "the" in out


def test_register_command_requires_explicit_yes_and_never_writes_without_it(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path)
    called = []
    monkeypatch.setattr(sonnet_cli, "_identity", lambda cfg: called.append("identity"))
    assert sonnet_cli.main(["--config", str(cfg), "register"]) == 2
    assert "--yes" in capsys.readouterr().err and called == []


def test_play_live_requires_yes_and_loads_nothing_otherwise(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path)
    called = []
    monkeypatch.setattr(sonnet_cli, "_identity", lambda cfg: called.append("identity"))
    assert sonnet_cli.main(["--config", str(cfg), "play", "xav", "--live"]) == 2
    assert "--yes" in capsys.readouterr().err and called == []


def test_poem_state_command_reconstructs_from_room(tmp_path, capsys, monkeypatch):
    from technocore_agent import identity
    from technocore_agent.client import Message, RoomPage
    cfg = _setup(tmp_path)
    ref = identity.load  # placeholder to keep import used
    referee = identity.create(tmp_path / "ref.pem", "pw")
    # la config de test pointe vers un autre DID arbitre : on la reecrit avec le notre
    text = (tmp_path / "sonnet.toml").read_text().replace(
        "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte", referee.did)
    (tmp_path / "sonnet.toml").write_text(text, "utf-8")
    room = "d-sonnet-2-team-xav"
    setup = json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": "s", "game_id": "xav",
                        "poem_room": room, "room_generation": 1, "state_hash": "h0", "sender_did": referee.did})
    prop = json.dumps({"type": "sonnet.word.v1", "word": "moon", "request_id": "p1"})
    other = identity.create(tmp_path / "o.pem", "pw")
    rec = json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": "p1", "sender_did": other.did,
                      "version": 1, "state_hash": "h1", "syllables": 1, "complete": False})
    msgs = [Message(1, "t", referee.did, setup, 1, referee.sign(room, 1, setup)),
            Message(2, "t", other.did, prop, 1, other.sign(room, 1, prop)),
            Message(3, "t", referee.did, rec, 2, referee.sign(room, 2, rec))]

    class FakeClient:
        def __init__(self, *a, **k):
            self.done = False

        def read(self, room, since=None, limit=200, wait=None):
            if self.done or since:
                return RoomPage(room, None, None, [], generation=1)
            self.done = True
            return RoomPage(room, 1, 3, msgs, generation=1)

    monkeypatch.setattr(sonnet_cli, "TechnocoreClient", FakeClient)
    assert sonnet_cli.main(["--config", str(cfg), "poem-state", "xav"]) == 0
    out = capsys.readouterr().out
    assert "version 1" in out and "moon" in out and "h1" in out
