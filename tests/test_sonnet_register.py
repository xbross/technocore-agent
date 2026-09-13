"""Inscription : JSON exact des regles, ecriture signee UNE fois, attente du recu signe par l'arbitre."""
import json

import pytest

from technocore_agent import identity
from technocore_agent.client import Message, RoomPage, SayResult
from technocore_agent.sonnet import register
from technocore_agent.sonnet.config import SonnetConfig

ROOM = "mb-sonnet-2-registration"
TOML = """
[contest]
id = "sonnet-2"
referee_did = "%s"
[rooms]
registration = "mb-sonnet-2-registration"
[participant]
role = "writer"
x_account_url = "https://x.com/rektbycryptos"
armed = %s
"""


def _cfg(tmp_path, referee, armed="true"):
    p = tmp_path / "sonnet.toml"
    p.write_text(TOML % (referee, armed), "utf-8")
    return SonnetConfig.load(p)


def test_registration_message_is_compact_json_with_rule_fields(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    text = register.registration_message(_cfg(tmp_path, ref.did), "register-1")
    assert json.loads(text) == {"type": "sonnet.register.v1", "contest_id": "sonnet-2", "role": "writer",
                                "x_account_url": "https://x.com/rektbycryptos", "request_id": "register-1"}
    assert "\n" not in text and ": " not in text and text.isascii()


def test_voter_registration_omits_x_account_url(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    cfg = _cfg(tmp_path, ref.did)
    cfg.role = "voter"
    assert "x_account_url" not in json.loads(register.registration_message(cfg, "r"))


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.said = []

    def say_signed(self, ident, room, text, nonce):
        self.said.append((room, text, nonce))
        return SayResult(body="", seq=1000, verified=True)

    def read(self, room, since=None, limit=200):
        return self.pages.pop(0) if self.pages else RoomPage(room, None, None, [], generation=1)


def test_register_refuses_when_disarmed(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    me = identity.create(tmp_path / "me.pem", "pw")
    client = FakeClient([])
    with pytest.raises(register.Disarmed):
        register.register(client, me, _cfg(tmp_path, ref.did, armed="false"), "register-1", wait_seconds=0)
    assert client.said == []


def test_register_posts_once_and_returns_matching_referee_receipt(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    me = identity.create(tmp_path / "me.pem", "pw")
    cfg = _cfg(tmp_path, ref.did)
    other_receipt = json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": "register-1",
                                "sender_did": "did:key:z6MkSomeoneElse"})
    mine = json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "role": "writer",
                       "request_id": "register-1", "sender_did": me.did, "reason": ""})
    fake_attacker = identity.create(tmp_path / "atk.pem", "pw")
    pages = [
        RoomPage(ROOM, 1001, 1002, [
            Message(1001, "t", fake_attacker.did, mine, 1, fake_attacker.sign(ROOM, 1, mine)),  # pas l'arbitre
            Message(1002, "t", ref.did, other_receipt, 1, ref.sign(ROOM, 1, other_receipt)),
        ], generation=1),
        RoomPage(ROOM, 1003, 1003, [Message(1003, "t", ref.did, mine, 2, ref.sign(ROOM, 2, mine))], generation=1),
    ]
    client = FakeClient(pages)
    receipt = register.register(client, me, cfg, "register-1", wait_seconds=10, sleep=lambda s: None)
    assert receipt["status"] == "accepted" and receipt["sender_did"] == me.did
    assert len(client.said) == 1 and client.said[0][0] == ROOM
    assert json.loads(client.said[0][1])["request_id"] == "register-1"


def test_register_returns_none_when_no_receipt_in_time(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    me = identity.create(tmp_path / "me.pem", "pw")
    client = FakeClient([])
    clock = iter([0, 0, 5, 11, 12, 13])
    assert register.register(client, me, _cfg(tmp_path, ref.did), "r", wait_seconds=10,
                             sleep=lambda s: None, now=lambda: next(clock)) is None
