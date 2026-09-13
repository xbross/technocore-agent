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
