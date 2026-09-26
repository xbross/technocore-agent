import close1_cli


def test_live_sell_requires_yes_and_loads_no_key(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("aucune cle ne doit etre chargee sans --yes")
    monkeypatch.setattr(close1_cli.identity, "load", boom)
    assert close1_cli.main(["sell", "--live"]) == 2
    assert "--yes" in capsys.readouterr().err


def test_status_reads_only_the_local_ledger(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(close1_cli, "LEDGER", tmp_path / "l.json")
    assert close1_cli.main(["status"]) == 0
    assert '"filled": "0"' in capsys.readouterr().out
