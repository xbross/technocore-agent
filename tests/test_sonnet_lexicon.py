"""Alphabet DID, mots jouables, rimes : la regle vient de sonnet_validate.py (paquet epingle)."""
import pytest

from technocore_agent.sonnet import lexicon

DID = "did:key:z6MknPooKck2cXu52NMM9h3cmeSxBzi2KkZEaHUSk3SwzfDk"
FAKE_LEXICON = {"and": 1, "the": 1, "ocean": 2, "o'clock": 2, "moon": 1, "soon": 1, "mind": 1}
FAKE_PRONS = {"moon": ["M", "UW1", "N"], "soon": ["S", "UW1", "N"], "mind": ["M", "AY1", "N", "D"],
              "ocean": ["OW1", "SH", "AH0", "N"]}


def test_did_alphabet_uses_full_lowercased_did():
    letters = lexicon.did_alphabet(DID)
    assert letters == set("abcdefhikmnopsuwxyz")
    assert {"d", "i", "k", "e", "y"} <= letters  # prefixe did:key: inclus


def test_did_alphabet_rejects_malformed_did():
    with pytest.raises(ValueError):
        lexicon.did_alphabet("did:key:z6Mkshort")


def test_playable_words_filters_by_alphabet_and_keeps_syllables():
    words = lexicon.playable_words(FAKE_LEXICON, lexicon.did_alphabet(DID))
    assert words == {"and": 1, "ocean": 2, "moon": 1, "soon": 1, "mind": 1}  # the: t ; o'clock: l


def test_rhyme_key_groups_words_by_final_stressed_vowel_and_coda():
    assert lexicon.rhyme_key(FAKE_PRONS["moon"]) == lexicon.rhyme_key(FAKE_PRONS["soon"])
    assert lexicon.rhyme_key(FAKE_PRONS["moon"]) != lexicon.rhyme_key(FAKE_PRONS["mind"])


def test_rhyme_families_only_list_playable_words():
    fams = lexicon.rhyme_families({"moon": 1, "soon": 1, "mind": 1}, FAKE_PRONS)
    assert sorted(fams[lexicon.rhyme_key(FAKE_PRONS["moon"])]) == ["moon", "soon"]


def test_read_pronunciations_takes_first_variant(tmp_path):
    p = tmp_path / "cmudict.dict"
    p.write_text(";;; comment\nmoon M UW1 N\nmoon(2) M UW0 N\nread R EH1 D # comment\n", "utf-8")
    prons = lexicon.read_pronunciations(p)
    assert prons["moon"] == ["M", "UW1", "N"] and prons["read"] == ["R", "EH1", "D"]


def test_roster_coverage_reports_union_missing_and_key_words():
    other = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
    cov = lexicon.roster_coverage([DID, other], {"the": 1, "and": 1, "vague": 1})
    assert cov.letters == lexicon.did_alphabet(DID) | lexicon.did_alphabet(other)
    assert cov.missing == set("abcdefghijklmnopqrstuvwxyz") - cov.letters
    assert "the" in cov.key_words_available and "vague" in cov.key_words_missing


def test_pitch_states_letter_count_and_missing_letters():
    text = lexicon.pitch(DID, {"and": 1, "moon": 1, "ocean": 2}, FAKE_PRONS)
    assert "19 letters" in text and "missing: g j l q r t v" in text
    assert text.isascii()


def test_pitch_strong_words_come_from_common_vocabulary_not_dictionary_noise():
    playable = {"a's": 1, "abbs": 1, "aase": 1, "moon": 1, "hope": 1, "shadow": 2, "and": 1}
    prons = dict(FAKE_PRONS, **{"hope": ["HH", "OW1", "P"], "shadow": ["SH", "AE1", "D", "OW0"],
                                "a's": ["EY1", "Z"], "abbs": ["AE1", "B", "Z"], "aase": ["AA1", "S"]})
    text = lexicon.pitch("did:key:z6MknPooKck2cXu52NMM9h3cmeSxBzi2KkZEaHUSk3SwzfDk", playable, prons)
    assert "moon" in text and "hope" in text
    assert "a's" not in text and "abbs" not in text and "aase" not in text
