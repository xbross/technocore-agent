import base64
import stat

import pytest

from technocore_agent import identity
from technocore_agent.base58 import b58decode, b58encode


def test_base58_known_vectors():
    assert b58encode(b"") == ""
    assert b58encode(b"\x00\x00abc") == "11ZiCa"
    assert b58encode(b"Hello World") == "JxF12TrwUP45BMd"
    assert b58decode("JxF12TrwUP45BMd") == b"Hello World"
    assert b58decode("11ZiCa") == b"\x00\x00abc"
    with pytest.raises(ValueError):
        b58decode("0OIl")


def test_did_matches_public_test_vector():
    # Vecteur du spec did:key (Ed25519) : cle publique -> DID connu.
    pub = bytes.fromhex("3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29")
    did = identity.did_from_public_bytes(pub)
    assert did == "did:key:z6MkiTBz1ymuepAQ4HEHYSF1H8quG5GLVVQR3djdX3mDooWp"
    assert identity.public_bytes_from_did(did) == pub


def test_create_load_sign_verify(tmp_path):
    path = tmp_path / "k.pem"
    ident = identity.create(path, "secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    pem = path.read_bytes()
    assert b"ENCRYPTED" in pem  # jamais en clair

    with pytest.raises(identity.IdentityError):
        identity.create(path, "secret")  # refuse d'ecraser
    with pytest.raises(identity.IdentityError):
        identity.load(path, "wrong")

    loaded = identity.load(path, "secret")
    assert loaded.did == ident.did
    assert loaded.did.startswith("did:key:z6Mk")
    assert len(loaded.fingerprint) == 16

    sig = loaded.sign("lobby", 1788898680268, "hello world")
    assert len(sig) == 86 and "=" not in sig and sig[-1] in "AQgw"
    assert identity.verify(loaded.did, "lobby", 1788898680268, "hello world", sig)
    assert not identity.verify(loaded.did, "lobby", 1788898680269, "hello world", sig)
    assert not identity.verify(loaded.did, "lobby", 1788898680268, "hello worlds", sig)


def test_verify_real_server_record():
    # Enregistrement reel lu dans /r/lobby?format=json le 2026-09-08.
    did = "did:key:z6MkpzpQvjPPbhfNhnYzYnu4xwBsanTiLQs7BDHjBJE5XQUg"
    text = "Another day, another check-in. The decentralized AI vision is compelling. · 91o5i"
    sig = "HuTon564VqkCR2V-ssjHlaZi12ZzrkaoWxddq_Hn4RAOVMTGStXGT8wXl8-Mm3nVfvmwtIts7K6U8lZpmhjHDQ"
    assert identity.verify(did, "lobby", 1788898680268, text, sig)


def test_bad_permissions_refused(tmp_path):
    path = tmp_path / "k.pem"
    identity.create(path, "s")
    path.chmod(0o644)
    with pytest.raises(identity.IdentityError):
        identity.load(path, "s")
