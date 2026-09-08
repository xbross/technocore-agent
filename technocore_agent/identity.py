"""Identite Ed25519 de l'agent : cle chiffree sur disque, DID, empreinte, signature.

La cle privee n'existe jamais en clair sur le disque : elle est serialisee en PEM
PKCS8 chiffre (BestAvailableEncryption) et ne vit qu'en memoire une fois chargee.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Multicodec ed25519-pub (0xed) en varint = 0xed 0x01
ED25519_MULTICODEC = b"\xed\x01"
DID_PREFIX = "did:key:z"


class IdentityError(Exception):
    """Erreur liee a la cle de l'agent (fichier manquant, passphrase fausse, ...)."""


def did_from_public_bytes(public_bytes: bytes) -> str:
    from .base58 import b58encode

    if len(public_bytes) != 32:
        raise IdentityError(f"cle publique Ed25519 attendue sur 32 octets, recu {len(public_bytes)}")
    return DID_PREFIX + b58encode(ED25519_MULTICODEC + public_bytes)


def public_bytes_from_did(did: str) -> bytes:
    from .base58 import b58decode

    if not did.startswith(DID_PREFIX):
        raise IdentityError(f"DID non supporte (attendu {DID_PREFIX}...): {did}")
    raw = b58decode(did[len(DID_PREFIX):])
    if not raw.startswith(ED25519_MULTICODEC) or len(raw) != 34:
        raise IdentityError(f"DID non Ed25519 ou mal forme: {did}")
    return raw[2:]


def fingerprint_of(did: str) -> str:
    """16 premiers caracteres hex de SHA-256(did) — convention technocore."""
    return hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]


def signing_payload(room: str, nonce: int, text: str) -> bytes:
    return f"{room}|{nonce}|{text}".encode("utf-8")


def verify(did: str, room: str, nonce: int, text: str, sig_b64url: str) -> bool:
    """Verifie une signature telle que servie par le serveur. True si valide."""
    from cryptography.exceptions import InvalidSignature

    try:
        sig = base64.urlsafe_b64decode(sig_b64url + "=" * (-len(sig_b64url) % 4))
        Ed25519PublicKey.from_public_bytes(public_bytes_from_did(did)).verify(
            sig, signing_payload(room, nonce, text)
        )
        return True
    except (InvalidSignature, ValueError, IdentityError):
        return False


@dataclass(frozen=True)
class Identity:
    _private_key: Ed25519PrivateKey

    @property
    def public_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @property
    def did(self) -> str:
        return did_from_public_bytes(self.public_bytes)

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.did)

    @property
    def short(self) -> str:
        """Forme courte 'z6Mk...XXXX' comme le serveur l'affiche."""
        body = self.did[len("did:key:"):]
        return f"{body[:4]}...{body[-4:]}"

    def sign(self, room: str, nonce: int, text: str) -> str:
        """Signature de `room|nonce|text`, base64url sans padding (86 caracteres)."""
        raw = self._private_key.sign(signing_payload(room, nonce, text))
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def create(path: Path, passphrase: str) -> Identity:
    """Genere une nouvelle cle et l'ecrit chiffree avec permissions 0600.

    Refuse d'ecraser un fichier existant : perdre une identite est irreversible.
    """
    if not passphrase:
        raise IdentityError("passphrase vide refusee")
    path = Path(path)
    if path.exists():
        raise IdentityError(f"{path} existe deja — supprimez-le explicitement si vous voulez une nouvelle identite")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    os.chmod(path, 0o600)
    return Identity(key)


def load(path: Path, passphrase: str) -> Identity:
    path = Path(path)
    if not path.exists():
        raise IdentityError(f"cle introuvable: {path} (lancez d'abord: python agent.py init)")
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise IdentityError(f"{path} a les permissions {mode:o}, attendu 600 (chmod 600 {path})")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=passphrase.encode("utf-8"))
    except (ValueError, TypeError) as e:
        raise IdentityError(f"impossible de dechiffrer {path}: passphrase incorrecte ? ({e})") from e
    if not isinstance(key, Ed25519PrivateKey):
        raise IdentityError(f"{path} ne contient pas une cle Ed25519")
    return Identity(key)


def passphrase_from_env_or_prompt(prompt: str = "Passphrase de la cle: ") -> str:
    """Ordre : TECHNOCORE_PASSPHRASE, puis le trousseau macOS (TECHNOCORE_PASSPHRASE_KEYCHAIN =
    nom du service enregistre avec `security add-generic-password`), sinon saisie masquee."""
    value = os.environ.get("TECHNOCORE_PASSPHRASE")
    if value:
        return value
    service = os.environ.get("TECHNOCORE_PASSPHRASE_KEYCHAIN")
    if service:
        import subprocess

        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise IdentityError(f"trousseau: aucun mot de passe pour le service {service!r} ({proc.stderr.strip()})")
        return proc.stdout.rstrip("\n")
    import getpass

    return getpass.getpass(prompt)
