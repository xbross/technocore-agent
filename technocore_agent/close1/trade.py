"""close-1 : termes d'echange et signatures emetteur/preneur.

Schema verifie le 26/09 sur des echanges regles par l'arbitre : l'emetteur signe
'close-1|terms|<termes>', le preneur 'close-1|accept|<termes>|<did du preneur>', les termes etant
le JSON a cles triees et sans espaces. Ed25519 brut, base64url sans padding (86 caracteres)."""
from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP

from .. import identity

SEASON = "close-1"
TRADE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
TWO_PLACES = re.compile(r"[0-9]{1,7}(\.[0-9]{1,2})?")
DID = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}")
MIN_QTY = Decimal("0.1")
CENT = Decimal("0.01")
KEYS = {"id", "maker", "px", "qty", "side", "taker", "until"}


def amount(s) -> Decimal | None:
    """Montant decimal a deux decimales au plus, tel que l'accepte le fold ; None sinon."""
    if not isinstance(s, str) or not TWO_PLACES.fullmatch(s):
        return None
    return Decimal(s)


def money(x: Decimal) -> str:
    return format(Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP), "f")


def make_terms(tid: str, maker: str, side: str, qty: Decimal, px: Decimal, taker: str, until: int) -> dict:
    """Termes valides pour le fold, ou ValueError (jamais d'arrondi silencieux)."""
    qty, px = Decimal(qty), Decimal(px)
    if not isinstance(tid, str) or not TRADE_ID.fullmatch(tid):
        raise ValueError(f"id invalide: {tid!r}")
    if side not in ("buy", "sell"):
        raise ValueError(f"cote invalide: {side!r}")
    if not DID.fullmatch(maker) or not (taker == "any" or DID.fullmatch(taker)):
        raise ValueError("maker/taker: did:key attendu")
    if type(until) is not int:
        raise ValueError("until: entier attendu")
    for name, v in (("qty", qty), ("px", px)):
        if v != v.quantize(CENT) or v <= 0:
            raise ValueError(f"{name}: deux decimales au plus et positif ({v})")
    if qty < MIN_QTY:
        raise ValueError(f"qty: au moins {MIN_QTY}")
    terms = {"id": tid, "maker": maker, "px": money(px), "qty": money(qty), "side": side, "taker": taker, "until": until}
    if amount(terms["px"]) is None or amount(terms["qty"]) is None:
        raise ValueError("montant hors format")
    return terms


def terms_json(terms: dict) -> str:
    return json.dumps(terms, sort_keys=True, separators=(",", ":"))


def _maker_payload(terms: dict) -> bytes:
    return f"{SEASON}|terms|{terms_json(terms)}".encode("utf-8")


def _taker_payload(terms: dict, taker_did: str) -> bytes:
    return f"{SEASON}|accept|{terms_json(terms)}|{taker_did}".encode("utf-8")


def sign_maker(ident, terms: dict) -> str:
    return ident.sign_raw(_maker_payload(terms))


def sign_taker(ident, terms: dict, taker_did: str) -> str:
    return ident.sign_raw(_taker_payload(terms, taker_did))


def verify_maker(terms: dict, sig: str) -> bool:
    return isinstance(terms.get("maker"), str) and identity.verify_raw(terms["maker"], _maker_payload(terms), sig)


def verify_taker(terms: dict, taker_did: str, sig: str) -> bool:
    return identity.verify_raw(taker_did, _taker_payload(terms, taker_did), sig)


def offer_text(terms: dict, maker_sig: str) -> str:
    """Offre ouverte (format observe dans close1) : ignoree par l'arbitre tant qu'un preneur ne la contresigne pas."""
    return ('{"t":"trade","season":"%s","terms":%s,"taker":"any","maker_sig":"%s"}'
            % (SEASON, terms_json(terms), maker_sig))


def trade_text(terms: dict, taker_did: str, maker_sig: str, taker_sig: str) -> str:
    return ('{"t":"trade","season":"%s","terms":%s,"taker":"%s","maker_sig":"%s","taker_sig":"%s"}'
            % (SEASON, terms_json(terms), taker_did, maker_sig, taker_sig))
