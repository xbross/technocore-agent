import pytest

from technocore_agent.sonnet.config import SonnetConfig, ConfigError

TOML = """
[contest]
id = "sonnet-2"
referee_did = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
deadline = "2026-09-18T12:00:00Z"

[rooms]
rules = "d-sonnet-2-rules"
registration = "mb-sonnet-2-registration"
discovery = "mb-sonnet-2-discovery"

[package]
dir = "contest/package"
commit = "e1999094c359ef7390bdf07fe2a151393a5c2f51"
[package.sha256]
"cmudict.dict" = "81917843c7f44ce2b094ac63873c2c7a4cf802040792c455ba3ca406891c3d22"

[participant]
role = "writer"
x_account_url = "https://x.com/rektbycryptos"

[watch]
poll_seconds = 15
archive_dir = "state/sonnet-archive"
"""


def test_load_config_reads_sections_and_defaults_to_disarmed(tmp_path):
    p = tmp_path / "sonnet.toml"
    p.write_text(TOML, "utf-8")
    cfg = SonnetConfig.load(p)
    assert cfg.contest_id == "sonnet-2"
    assert cfg.referee_did.endswith("AAMzte")
    assert cfg.rooms["discovery"] == "mb-sonnet-2-discovery"
    assert cfg.package_sha256["cmudict.dict"].startswith("81917843")
    assert cfg.x_account_url == "https://x.com/rektbycryptos"
    assert cfg.armed is False
    assert cfg.poll_seconds == 15
    assert cfg.archive_dir == tmp_path / "state/sonnet-archive"  # relatif au fichier


def test_config_rejects_malformed_referee_or_x_url(tmp_path):
    p = tmp_path / "sonnet.toml"
    p.write_text(TOML.replace("did:key:z6MkowHQ", "did:key:z6Mkow"), "utf-8")
    with pytest.raises(ConfigError, match="referee_did"):
        SonnetConfig.load(p)
    p.write_text(TOML.replace("https://x.com/rektbycryptos", "x.com/rektbycryptos"), "utf-8")
    with pytest.raises(ConfigError, match="x_account_url"):
        SonnetConfig.load(p)
