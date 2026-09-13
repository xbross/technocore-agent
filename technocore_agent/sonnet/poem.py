"""Machine d'etat d'un poeme, reconstruite depuis la room d'equipe.

Seuls comptent les recus signes par le DID arbitre epingle (verification locale de la
signature). Une proposition `sonnet.word.v1` n'entre dans le poeme que si un recu arbitre
`accepted` porte son request_id. Tout autre texte de la room est une donnee ignoree.

Formats observes le 2026-09-13 dans des rooms d'equipe reelles (d-sonnet-2-team-quill/bub) :
  recu mot  : {"complete":false,"request_id":...,"sender_did":...,"state_hash":...,"status":"accepted",
               "syllables":64,"type":"sonnet.receipt.v1","version":48}
  recu setup: {"game_id":...,"poem_room":...,"room_generation":2,"state_hash":...,"status":"accepted"}
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..client import Message
from .lexicon import rhyme_key, letters_of
from .watch import classify

LINE_SYLLABLES = 10
LINES = 14
STANZAS = (4, 4, 4, 2)
# ABAB CDCD EFEF GG : premiere ligne de chaque famille de rime
FAMILY_STARTS = (0, 1, 4, 5, 8, 9, 12)


def rhyme_partner(line_index: int) -> int | None:
    """Ligne anterieure avec laquelle la ligne doit rimer (schema ABAB CDCD EFEF GG)."""
    if line_index == 13:
        return 12
    if line_index >= 12:
        return None
    pos = line_index % 4
    return line_index - 2 if pos >= 2 else None


def word_message(contest_id: str, game_id: str, generation: int, version: int, previous_state_hash: str,
                 word: str, request_id: str) -> str:
    body = {"type": "sonnet.word.v1", "contest_id": contest_id, "game_id": game_id,
            "room_generation": generation, "version": version, "previous_state_hash": previous_state_hash,
            "word": word, "request_id": request_id}
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True)


def canonical_text(lines: list[list[str]]) -> str:
    """Un espace entre mots, LF entre lignes, ligne vide entre strophes 4/4/4/2, pas de LF final."""
    out, i = [], 0
    for size in STANZAS:
        out.append("\n".join(" ".join(line) for line in lines[i:i + size]))
        i += size
    return "\n\n".join(out)


def poem_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_word(token: str) -> str:
    """Mot sans la ponctuation finale autorisee, en minuscules (cle du lexique)."""
    return token.rstrip(",.;:!?").lower()


@dataclass
class AcceptedWord:
    word: str
    sender: str
    version: int
    syllables_after: int


@dataclass
class PoemState:
    game_id: str
    room: str
    generation: int | None = None
    version: int = 0
    state_hash: str | None = None
    syllables: int = 0
    complete: bool = False
    words: list[AcceptedWord] = field(default_factory=list)
    lexicon: dict[str, int] = field(default_factory=dict)
    last_seq: int = 0

    @property
    def last_contributor(self) -> str | None:
        return self.words[-1].sender if self.words else None

    @property
    def line_index(self) -> int:
        return min(self.syllables // LINE_SYLLABLES, LINES - 1)

    @property
    def remaining(self) -> int:
        return LINE_SYLLABLES - self.syllables % LINE_SYLLABLES

    def can_play(self, did: str) -> bool:
        return not self.complete and self.state_hash is not None and self.last_contributor != did

    def lines(self) -> list[list[str]]:
        """Lignes reconstruites depuis les compteurs cumules des recus (10 syllabes ferment une ligne)."""
        lines: list[list[str]] = []
        prev = 0
        for w in self.words:
            if prev % LINE_SYLLABLES == 0:
                lines.append([])
            lines[-1].append(w.word)
            prev = w.syllables_after
        return lines

    def apply(self, msg: Message, referee_did: str, proposals: dict[tuple[str, str], str]) -> None:
        kind = classify(msg, self.room, referee_did)
        if kind == "receipt":
            data = json.loads(msg.text)
            if data.get("status") != "accepted":
                return
            if "room_generation" in data and "version" not in data:
                self.generation = int(data["room_generation"])
            if "state_hash" in data:
                self.state_hash = str(data["state_hash"])
            if "version" in data:
                version = int(data["version"])
                key = (str(data.get("sender_did")), str(data.get("request_id")))
                word = proposals.get(key)
                if word is not None and version == self.version + 1:
                    syl = int(data.get("syllables", self.syllables))
                    self.words.append(AcceptedWord(word, key[0], version, syl))
                    self.syllables = syl
                self.version = version
                self.complete = bool(data.get("complete", False))
        elif kind == "other" and msg.signed:
            try:
                data = json.loads(msg.text)
            except ValueError:
                return
            if isinstance(data, dict) and data.get("type") == "sonnet.word.v1" and isinstance(data.get("word"), str):
                proposals[(msg.sender, str(data.get("request_id")))] = data["word"]

    @classmethod
    def from_messages(cls, messages: list[Message], room: str, referee_did: str, game_id: str,
                      lexicon: dict[str, int]) -> "PoemState":
        st = cls(game_id=game_id, room=room, lexicon=lexicon)
        proposals: dict[tuple[str, str], str] = {}
        for msg in sorted(messages, key=lambda m: m.seq):
            st.apply(msg, referee_did, proposals)
            st.last_seq = max(st.last_seq, msg.seq)
        return st


def candidates(lines: list[list[str]], playable: dict[str, int], prons: dict[str, list[str]],
               syllables_in_line: int | None = None) -> dict[str, int]:
    """Mots jouables pour l'etat courant : syllabes <= restantes ; si le mot ferme la ligne,
    il doit rimer avec sa ligne partenaire (ABAB...) et, s'il ouvre une famille, ne pas reprendre
    le son d'une famille existante. `lines` = lignes reconstruites, la derniere etant en cours
    (ou complete ; alors une nouvelle ligne s'ouvre)."""
    if not lines:
        lines = [[]]
    current = lines[-1]
    if syllables_in_line is None:
        syllables_in_line = sum(playable.get(strip_word(w), 0) or _syl(w, prons) for w in current)
    if syllables_in_line >= LINE_SYLLABLES:
        lines = lines + [[]]
        current = []
        syllables_in_line = 0
    line_index = len(lines) - 1
    remaining = LINE_SYLLABLES - syllables_in_line
    partner = rhyme_partner(line_index)
    required = None
    if partner is not None and partner < len(lines) - 1 and lines[partner]:
        last = strip_word(lines[partner][-1])
        required = rhyme_key(prons[last]) if last in prons else None
    used = set()
    if line_index in FAMILY_STARTS:
        for start in FAMILY_STARTS:
            if start < line_index and start < len(lines) - 1 and lines[start]:
                last = strip_word(lines[start][-1])
                if last in prons:
                    used.add(rhyme_key(prons[last]))
    out = {}
    for w, n in playable.items():
        if n > remaining:
            continue
        if n == remaining:
            key = rhyme_key(prons[w]) if w in prons else None
            if required is not None and key != required:
                continue
            if required is None and line_index in FAMILY_STARTS and key in used:
                continue
        out[w] = n
    return out


def _syl(word: str, prons: dict[str, list[str]]) -> int:
    ph = prons.get(strip_word(word), [])
    return sum(1 for p in ph if p[-1:].isdigit())
