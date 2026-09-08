"""Encodage base58btc (alphabet Bitcoin), sans dependance externe.

Utilise par did:key : `z` (prefixe multibase) + base58btc(multicodec + cle).
"""

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    """Encode des octets en base58btc. Les zeros de tete deviennent des '1'."""
    n = int.from_bytes(data, "big")
    out = []
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(ALPHABET[rem])
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading_zeros + "".join(reversed(out))


def b58decode(text: str) -> bytes:
    """Decode une chaine base58btc en octets. Leve ValueError si un caractere est invalide."""
    n = 0
    for ch in text:
        idx = ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"caractere base58 invalide: {ch!r}")
        n = n * 58 + idx
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    leading_ones = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_ones + body
