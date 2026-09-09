"""Filtre de sortie : derniere barriere avant qu'un texte parte signe sous notre DID.

Independant du cerveau (regles, claude-cli, claude-api) : une reponse qui contient un
lien, une adresse de portefeuille, du vocabulaire financier ou de secrets est refusee,
et la raison est journalisee. Un message pieges lu dans une room ne peut donc pas
transformer l'agent en relais d'arnaque, quel que soit le modele.
"""

from __future__ import annotations

import re

# Le service lui-meme et son manuel sont autorises ; tout autre lien ou domaine est refuse.
ALLOWED_HOSTS = ("technocore.chat",)
URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(com|net|org|io|xyz|app|finance|chat|me|link|top|site)\b\S*)", re.I)
WALLET_RE = re.compile(
    r"\b0x[0-9a-fA-F]{40}\b"            # EVM
    r"|\bbc1[a-z0-9]{25,60}\b"          # bitcoin bech32
    r"|\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b"  # bitcoin legacy
    r"|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"  # solana et autres base58 longs
)
FORBIDDEN_WORDS = [
    "airdrop", "snapshot", "send", "transfer", "wallet", "deposit", "withdraw", "wire",
    # 'trade', 'swap', 'stake' sont du vocabulaire courant du protocole (offres tclk, staking) :
    # seuls les usages incitatifs sont refuses, pas le mot seul.
    "invest", "buy", "sell", "trade with me", "swap now", "stake now", "claim your", "claim now", "claim free",
    "claim rewards?",
    "presale", "whitelist",
    "guaranteed", "profit", "seed phrase", "private key", "passphrase", "password", "api key",
    "pay me", "pay you", "payment required", "send payment", "usdt", "usdc", "eth", "btc", "sol",
]
FORBIDDEN_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in FORBIDDEN_WORDS) + r")s?\b", re.I)
# Tentatives de faire relayer une instruction a d'autres agents
INSTRUCTION_RE = re.compile(
    r"\b(ignore (all|previous|prior)|run this|execute|curl |fetch this|visit|click|dm me|contact me on)\b", re.I
)


def check_reply(text: str) -> str | None:
    """None si le texte peut partir, sinon la raison du refus."""
    if not text or not text.strip():
        return "texte vide"
    if not all(32 <= ord(c) < 127 for c in text):
        return "caracteres non ASCII"
    for m in URL_RE.finditer(text):
        host = re.sub(r"^(https?://|www\.)", "", m.group(0), flags=re.I).split("/")[0].lower().rstrip(".,;:)")
        if host not in ALLOWED_HOSTS:
            return f"contient un lien ou un nom de domaine ({host})"
    if WALLET_RE.search(text):
        return "ressemble a une adresse de portefeuille"
    m = FORBIDDEN_RE.search(text)
    if m:
        return f"vocabulaire interdit: {m.group(0)!r}"
    m = INSTRUCTION_RE.search(text)
    if m:
        return f"relaie une instruction: {m.group(0)!r}"
    return None


def looks_like_injection(text: str) -> bool:
    """Le message RECU contient-il un lien etranger ou une adresse de portefeuille ?
    Sert au blocage automatique : on ne bloque un emetteur que si son propre message est
    suspect, jamais pour un mot choisi par notre modele dans la reponse."""
    for m in URL_RE.finditer(text):
        host = re.sub(r"^(https?://|www\.)", "", m.group(0), flags=re.I).split("/")[0].lower().rstrip(".,;:)")
        if host not in ALLOWED_HOSTS:
            return True
    return bool(WALLET_RE.search(text))
