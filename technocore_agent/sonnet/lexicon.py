"""Alphabet DID et mots jouables.

La regle vient du validateur officiel epingle (sonnet_validate.py, ligne `allowed = ...`) :
les lettres autorisees sont celles du DID COMPLET en minuscules, prefixe `did:key:` inclus.
Le compte de syllabes officiel est le plus grand compte liste dans CMUdict pour le mot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Meme garde de forme que le validateur officiel.
ED25519_DID = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}")
WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)*")
VOWELS = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY", "OW", "OY", "UH", "UW"}
ALPHABET = set("abcdefghijklmnopqrstuvwxyz")

# Mots-outils dont l'absence pese le plus sur une equipe (choix editorial, pas une regle).
KEY_WORDS = ("the", "and", "of", "to", "a", "in", "is", "it", "you", "that", "for", "with", "are",
             "this", "not", "but", "from", "or", "have", "will", "my", "we", "love", "light", "night",
             "heart", "time", "all", "when", "what")

# Vocabulaire courant (choix editorial) : sert a presenter des mots lisibles dans le pitch,
# pas a restreindre le jeu. Tout mot CMUdict reste jouable pour le validateur.
COMMON_WORDS = frozenset("""
a about above ache across after again age ago air all alone along also always am among an and
another any are arm as ash ask at away back be beam bean bed bee been before begin behind below
bend beneath beside best between beyond bind bird black bless blind blood bloom blow blue body
bone book born both bound bow break breath breathe bright bring broken burn but by call calm came
can cannot care carry cast catch cause chain chance change chase cheek child choose city claim
clay clean clear climb close cloud coal coast cold come cool could count cover crown cry dance dark
dawn day dead dear death deep desire dew did die dim dine do does done door doubt down dream drink
drop dry dust dusk each ear earth ease east echo edge else end enough even evening ever every eye
face fade faint fair faith fall far fast fate fear feed feel few field find fine fire first fish
flame flesh floor flow flower fly foam fold follow food fool foot for form found free fresh friend
from frost full gaze gift give glass gleam glow go god gold gone good grace grass grave gray great
green grief grow guess hair half hand hang happy hard harm has haste hate have he head heal hear
heart heat heaven heavy held here hide high hill him his hold hollow holy home honey hope hour
house how hue human hunger hush i ice idle if in inside into iron is it join joy just keep kind
kiss knee knew know known lack lake lamp land last late laugh lay lead leaf learn leave less let
lie life light like line lip listen little live lone long look loose lose loss lost loud love low
made make man many mark may me mean meet memory mend mid mild mind mine mist moon more morning
moss most mother mouth move much music must my name near need never new night no noise none noon
nor north not note nothing now numb ocean of off often old on once one only open or other our out
over own pain pale part pass past path peace pen people pine place plain play poem pray press
pride pure quiet rain raise reach read red rest rhyme rich ride ring rise river road rock room
root rose round run rush sad safe said sail same sand save say sea season see seed seek seem seen
self send sense shade shadow shake shall shape share she shine ship shoe shore should show side
sigh sign silence silent silver sin since sing sink sit skin sky sleep slow small smile smoke snow
so soft soil some son song soon soul sound south sow space speak spend spin spirit spring stand
star stay steel step still stone stop storm story strange stream street strong such summer sun
sweet swim take tale tear tell than thank that the thee their them then there these they thin
thing think this those thou thought thousand three through thy tide time tired to today tomorrow
tone too touch toward town tree true trust truth try turn twice two under undone unknown until up
upon us use vain vast veil very voice wait wake walk wall wander want warm was wash waste watch
water wave way we weak wear weep well went were west wet what when where which while whisper
white who whole whose why wide wild will wind window wine wing winter wise wish with within
without woe woman wonder wood word work world worn would wound write wrong year yes yet you young
your youth zone
""".split())


def letters_of(text: str) -> set[str]:
    return {ch for ch in text.lower() if "a" <= ch <= "z"}


def did_alphabet(did: str) -> set[str]:
    """Lettres jouables pour un DID (regle officielle : DID complet en minuscules)."""
    if not isinstance(did, str) or not ED25519_DID.fullmatch(did):
        raise ValueError("DID attendu au format did:key:z6Mk + 44 caracteres base58")
    return letters_of(did)


def read_lexicon(path: Path) -> dict[str, int]:
    """Mot -> plus grand compte de syllabes (meme regle que read_lexicon officiel)."""
    counts: dict[str, int] = {}
    for entry in Path(path).read_text(encoding="utf-8").splitlines():
        fields = entry.split("#", 1)[0].split()
        if not fields or fields[0].startswith(";;;"):
            continue
        word = re.sub(r"\(\d+\)$", "", fields[0]).lower()
        if not WORD.fullmatch(word):
            continue
        count = sum(ph[:-1] in VOWELS and ph[-1:] in {"0", "1", "2"} for ph in fields[1:])
        if count:
            counts[word] = max(counts.get(word, 0), count)
    return counts


def read_pronunciations(path: Path) -> dict[str, list[str]]:
    """Mot -> phones de la PREMIERE variante (suffisant pour classer les rimes)."""
    prons: dict[str, list[str]] = {}
    for entry in Path(path).read_text(encoding="utf-8").splitlines():
        fields = entry.split("#", 1)[0].split()
        if not fields or fields[0].startswith(";;;"):
            continue
        word = fields[0].lower()
        if not WORD.fullmatch(word) or word in prons:
            continue  # "word(2)" ne matche pas WORD : seules les premieres variantes passent
        prons[word] = fields[1:]
    return prons


def playable_words(lexicon: dict[str, int], alphabet: set[str]) -> dict[str, int]:
    """Sous-ensemble du lexique dont toutes les lettres sont dans l'alphabet (apostrophes ignorees)."""
    return {w: n for w, n in lexicon.items() if letters_of(w) <= alphabet}


def rhyme_key(phones: list[str]) -> tuple[str, ...]:
    """Cle de rime : phones depuis la derniere voyelle accentuee (stress 1), sans les chiffres."""
    idx = None
    for i, ph in enumerate(phones):
        if ph[:-1] in VOWELS and ph[-1:] == "1":
            idx = i
    if idx is None:
        for i, ph in enumerate(phones):
            if ph[:-1] in VOWELS and ph[-1:].isdigit():
                idx = i
    if idx is None:
        return tuple(phones)
    return tuple(ph.rstrip("012") for ph in phones[idx:])


def rhyme_families(playable: dict[str, int], prons: dict[str, list[str]]) -> dict[tuple[str, ...], list[str]]:
    fams: dict[tuple[str, ...], list[str]] = {}
    for w in playable:
        if w in prons:
            fams.setdefault(rhyme_key(prons[w]), []).append(w)
    return fams


@dataclass
class RosterCoverage:
    letters: set[str]
    missing: set[str]
    key_words_available: list[str] = field(default_factory=list)
    key_words_missing: list[str] = field(default_factory=list)


def roster_coverage(dids: list[str], key_words: dict[str, int]) -> RosterCoverage:
    """Couverture d'un roster : union des alphabets et sort des mots-cles fournis."""
    union: set[str] = set()
    for d in dids:
        union |= did_alphabet(d)
    avail = [w for w in key_words if letters_of(w) <= union]
    missing = [w for w in key_words if not letters_of(w) <= union]
    return RosterCoverage(letters=union, missing=ALPHABET - union,
                          key_words_available=avail, key_words_missing=missing)


def pitch(did: str, playable: dict[str, int], prons: dict[str, list[str]], top: int = 12) -> str:
    """Texte ASCII de recrutement : lettres couvertes, manquantes, mots forts, rimes."""
    letters = did_alphabet(did)
    missing = sorted(ALPHABET - letters)
    common = {w: n for w, n in playable.items() if w in COMMON_WORDS}
    mono = sorted(w for w, n in common.items() if n == 1 and len(w) >= 3)
    functions = [w for w in KEY_WORDS if w in playable]
    fams = rhyme_families(common, prons)
    best = sorted(fams.values(), key=len, reverse=True)[:4]
    parts = [
        f"I cover {len(letters)} letters ({' '.join(sorted(letters))}), missing: {' '.join(missing)}.",
        f"Playable CMUdict words: {len(playable)} ({len(common)} everyday words).",
        f"Function words I can play: {' '.join(functions) or 'none'}.",
        f"Strong monosyllables: {' '.join(mono[:top])}.",
        "Rhyme families: " + " | ".join(" ".join(sorted(f)[:5]) for f in best) + ".",
    ]
    text = " ".join(parts)
    return text.encode("ascii", "ignore").decode("ascii")
