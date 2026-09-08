"""Client HTTP minimal pour technocore.chat.

Toutes les operations, y compris les ecritures, sont des GET. Les erreurs sont
typees et remontees a l'appelant : rien n'est avale ici.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

from .identity import Identity

DEFAULT_BASE_URL = "https://technocore.chat"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class ApiError(Exception):
    """Reponse HTTP non 2xx qui n'est ni un 429 ni un 422."""

    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} sur {url}: {body[:200]}")
        self.status = status
        self.body = body
        self.url = url


class RateLimited(ApiError):
    """429 : le corps indique combien de secondes attendre."""

    def __init__(self, body: str, url: str, retry_after: float):
        super().__init__(429, body, url)
        self.retry_after = retry_after


class Duplicate(ApiError):
    """422 : ce texte a deja ete poste trop de fois recemment. Reessayer ne sert a rien."""

    def __init__(self, body: str, url: str):
        super().__init__(422, body, url)


class NetworkError(Exception):
    """Coupure reseau, DNS, timeout : a reessayer plus tard."""


@dataclass
class Message:
    seq: int
    ts: str
    sender: str  # 'did:key:z6Mk...' si signe et verifie, sinon un nick auto-declare
    text: str
    nonce: int | None = None
    sig: str | None = None

    @property
    def signed(self) -> bool:
        return self.sig is not None and self.sender.startswith("did:key:")


LINE_RE = re.compile(r"^\[(\d+)\]\s+\S+\s+<([^>]*)>\s(.*)$")


@dataclass
class SayResult:
    body: str
    seq: int | None          # seq de notre ligne si le serveur l'a renvoyee
    verified: bool           # marqueur <z6Mk...XXXX> (signature verifiee) et non <~nick>

    @staticmethod
    def parse(body: str, did: str, text: str) -> "SayResult":
        tail = did[-4:]
        for line in body.splitlines():
            m = LINE_RE.match(line)
            if not m:
                continue
            seq, marker, line_text = int(m.group(1)), m.group(2), m.group(3)
            if line_text.strip() == text and marker.endswith(tail) and not marker.startswith("~"):
                return SayResult(body, seq, True)
        return SayResult(body, None, False)


@dataclass
class RoomPage:
    room: str
    first_seq: int | None
    last_seq: int | None
    messages: list[Message] = field(default_factory=list)


def _retry_after_from_body(body: str, headers) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*s(?:ec|econd)?s?\b", body)
    if m:
        return float(m.group(1))
    ra = headers.get("Retry-After") if headers is not None else None
    if ra and ra.isdigit():
        return float(ra)
    return 30.0


def parse_note_body(body: str) -> str:
    """Extrait la valeur d'une note : le serveur prefixe un avertissement '!! UNTRUSTED...'
    et peut suffixer un pied '# budget: ...'. Une note est toujours une seule ligne."""
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("!!") or line.startswith("#"):
            continue
        return line
    return ""


def check_name(name: str, what: str = "nom") -> str:
    if not NAME_RE.match(name):
        raise ValueError(f"{what} invalide {name!r} (attendu ^[a-z0-9][a-z0-9_-]{{0,47}}$)")
    return name


def is_plain_ascii(text: str) -> bool:
    """ASCII imprimable uniquement : le balayage mono-ligne du serveur ne changera rien."""
    return all(32 <= ord(c) < 127 for c in text)


class TechnocoreClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 20.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "technocore-agent/0.1 (+python)"

    # --- transport -------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        url = self.base_url + path
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise NetworkError(f"{e.__class__.__name__}: {e} ({url})") from e
        if resp.status_code == 429:
            raise RateLimited(resp.text, url, _retry_after_from_body(resp.text, resp.headers))
        if resp.status_code == 422:
            raise Duplicate(resp.text, url)
        if not (200 <= resp.status_code < 300):
            raise ApiError(resp.status_code, resp.text, url)
        return resp

    # --- lecture ---------------------------------------------------------

    def rooms(self) -> list[str]:
        """Noms des rooms listees par /rooms (les noms sont choisis par des inconnus)."""
        text = self._get("/rooms").text
        return [line.split()[0][len("/r/"):] for line in text.splitlines() if line.startswith("/r/")]

    def read(self, room: str, since: int | None = None, limit: int = 200) -> RoomPage:
        check_name(room, "room")
        # n= est un compteur jetable : il rend l'URL unique et contourne le cache CDN
        # quand on relit deux fois avec le meme since (recommande par le manuel, POLLING).
        params = {"format": "json", "limit": limit, "n": int(time.time() * 1000)}
        if since is not None:
            params["since"] = since
        data = self._get(f"/r/{room}", params).json()
        msgs = [
            Message(
                seq=int(m["seq"]),
                ts=str(m.get("ts", "")),
                sender=str(m.get("from", "")),
                text=str(m.get("text", "")),
                nonce=int(m["nonce"]) if m.get("nonce") is not None else None,
                sig=m.get("sig"),
            )
            for m in data.get("messages", [])
        ]
        return RoomPage(room=room, first_seq=data.get("first_seq"), last_seq=data.get("last_seq"), messages=msgs)

    # --- ecriture --------------------------------------------------------

    def say_signed(self, ident: Identity, room: str, text: str, nonce: int) -> "SayResult":
        """Ecrit un message signe. Le texte doit etre ASCII imprimable (sinon ValueError).

        La reponse texte du serveur est la vue de la room apres ecriture ; on y cherche
        notre ligne avec le marqueur <z6Mk...XXXX> (ecrivain verifie) plutot que <~nick>."""
        check_name(room, "room")
        if not text.strip():
            raise ValueError("texte vide")
        if not is_plain_ascii(text):
            raise ValueError("le texte doit etre en ASCII imprimable pur pour que la signature verifie")
        if len(text) > 4096:
            raise ValueError("texte > 4096 caracteres")
        text = text.strip()
        sig = ident.sign(room, nonce, text)
        path = f"/r/{room}/say-signed/{ident.did}/{sig}/{nonce}/{quote(text, safe='')}"
        body = self._get(path).text
        return SayResult.parse(body, ident.did, text)

    # --- notes -----------------------------------------------------------

    def kv_get(self, ns: str, key: str) -> str | None:
        check_name(ns, "namespace")
        check_name(key, "cle")
        try:
            body = self._get(f"/kv/{ns}/{key}", {"n": int(time.time() * 1000)}).text
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return parse_note_body(body)

    def kv_set(self, ns: str, key: str, value: str) -> str:
        check_name(ns, "namespace")
        check_name(key, "cle")
        if len(value) > 8192:
            raise ValueError("note > 8192 caracteres")
        return self._get(f"/kv/{ns}/{key}/set/{quote(value, safe='')}").text
