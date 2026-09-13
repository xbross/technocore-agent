"""Paquet officiel epingle : rien n'est lu ni importe avant verification des sha256.

Les hashes de reference viennent du record de lancement signe par l'arbitre
(d-sonnet-2-rules seq 1, recopie dans LAUNCH.md du repo officiel) et sont
recopies dans sonnet.toml. Un fichier manquant ou modifie => PackageError.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Callable

RAW_BASE = "https://raw.githubusercontent.com/flop-labs/technocore-sonnet-challenge/"


class PackageError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_package(directory: Path, expected: dict[str, str]) -> None:
    directory = Path(directory)
    if not expected:
        raise PackageError("aucun hash attendu : sonnet.toml [package.sha256] est vide")
    for name, digest in expected.items():
        path = directory / name
        if not path.is_file():
            raise PackageError(f"{name}: fichier absent dans {directory}")
        actual = sha256_file(path)
        if actual != digest.lower():
            raise PackageError(f"{name}: sha256 {actual[:16]}... differe de la reference {digest[:16]}...")


def load_validator(directory: Path, expected: dict[str, str], name: str = "sonnet_validate.py"):
    """Importe le validateur officiel APRES verification de tout le paquet."""
    verify_package(directory, expected)
    if name not in expected:
        raise PackageError(f"{name}: pas de hash de reference, import refuse")
    path = Path(directory) / name
    spec = importlib.util.spec_from_file_location("sonnet_official_validate", path)
    if spec is None or spec.loader is None:
        raise PackageError(f"{name}: import impossible")
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop("sonnet_official_validate", None)
    spec.loader.exec_module(module)
    return module


def fetch_package(directory: Path, commit: str, expected: dict[str, str],
                  fetch: Callable[[str], bytes] | None = None) -> None:
    """Telecharge les fichiers epingles au commit donne, puis verifie. Ecrit dans un .tmp d'abord."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if fetch is None:
        import requests

        def fetch(url: str) -> bytes:  # noqa: E306
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return r.content
    for name in expected:
        data = fetch(f"{RAW_BASE}{commit}/{name}")
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected[name].lower():
            raise PackageError(f"{name}: le fichier telecharge ({digest[:16]}...) ne correspond pas a la reference")
        tmp = directory / (name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(directory / name)
    verify_package(directory, expected)
