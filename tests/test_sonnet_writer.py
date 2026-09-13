"""Le joueur : choisit un mot dans une liste filtree, ne poste qu'arme, confirme le recu arbitre."""
import json

from technocore_agent import identity
from technocore_agent.client import Message, RoomPage, SayResult
from technocore_agent.sonnet import poem, writer

ROOM = "d-sonnet-2-team-xav"
LEX = {"moon": 1, "shine": 1, "ocean": 2, "and": 1, "soon": 1, "mind": 1, "we": 1, "the": 1}
PRONS = {"moon": ["M", "UW1", "N"], "soon": ["S", "UW1", "N"], "shine": ["SH", "AY1", "N"],
         "mind": ["M", "AY1", "N", "D"], "ocean": ["OW1", "SH", "AH0", "N"], "and": ["AH0", "N", "D"],
         "we": ["W", "IY1"], "the": ["DH", "AH0"]}
MY = "did:key:z6MknPooKck2cXu52NMM9h3cmeSxBzi2KkZEaHUSk3SwzfDk"  # sans t : 'the' injouable


class FakeClient:
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.said = []

    def say_signed(self, ident, room, text, nonce):
        self.said.append((room, text, nonce))
        return SayResult("", 42, True)

    def read(self, room, since=None, limit=200, wait=None):
        return self.pages.pop(0) if self.pages else RoomPage(room, None, None, [], generation=1)


class PickBrain:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def choose(self, context, shortlist):
        self.calls.append((context, list(shortlist)))
        return self.answer


def _state(last=None, syllables=0):
    st = poem.PoemState(game_id="xav", room=ROOM, generation=1, version=3, state_hash="h3", lexicon=LEX)
    if last:
        st.words.append(poem.AcceptedWord("and", last, 3, syllables))
        st.syllables = syllables
    return st


def _writer(tmp_path, client, brain, dry_run=True, my=None):
    ident = my or identity.load if False else None
    me = identity.create(tmp_path / "me.pem", "pw")
    playable = {w: n for w, n in LEX.items() if set(c for c in w if c.isalpha()) <= set(me.did.lower())}
    w = writer.Writer(client, me, contest_id="sonnet-2", referee_did="did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte",
                      playable=playable, prons=PRONS, brain=brain, archive_dir=tmp_path / "arc", dry_run=dry_run)
    return w, me


def test_dry_run_builds_the_exact_message_but_never_posts(tmp_path):
    client = FakeClient()
    w, me = _writer(tmp_path, client, PickBrain("moon"))
    st = _state(last="did:key:z6MkSomeoneElse", syllables=1)
    if "moon" not in w.playable:
        w.playable["moon"] = 1
    prop = w.turn(st)
    assert prop is not None and prop.posted is False
    assert json.loads(prop.text)["word"] == "moon" and json.loads(prop.text)["version"] == 3
    assert json.loads(prop.text)["previous_state_hash"] == "h3"
    assert client.said == []


def test_no_turn_when_we_were_the_previous_contributor(tmp_path):
    client = FakeClient()
    w, me = _writer(tmp_path, client, PickBrain("moon"))
    assert w.turn(_state(last=me.did, syllables=1)) is None


def test_brain_answer_outside_shortlist_falls_back_to_top_candidate(tmp_path):
    client = FakeClient()
    brain = PickBrain("the")
    w, me = _writer(tmp_path, client, brain)
    w.playable = {"moon": 1, "ocean": 2}
    prop = w.turn(_state(last="did:key:z6MkX", syllables=1))
    assert json.loads(prop.text)["word"] in ("moon", "ocean")
    assert "the" not in brain.calls[0][1]


def test_armed_turn_posts_once_and_reads_back_receipt(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    client = FakeClient()
    w, me = _writer(tmp_path, client, PickBrain("moon"), dry_run=False)
    w.referee_did = ref.did
    w.playable = {"moon": 1}
    st = _state(last="did:key:z6MkX", syllables=1)
    prop = w.turn(st)
    assert prop.posted and len(client.said) == 1 and client.said[0][0] == ROOM
    rid = json.loads(prop.text)["request_id"]
    receipt = json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": rid, "sender_did": me.did,
                          "version": 4, "state_hash": "h4", "syllables": 2, "complete": False})
    page = RoomPage(ROOM, 50, 50, [Message(50, "t", ref.did, receipt, 9, ref.sign(ROOM, 9, receipt))], generation=1)
    client.pages = [page]
    outcome = w.await_receipt(st, prop, wait_seconds=5, sleep=lambda s: None, now=iter([0, 1, 2]).__next__)
    assert outcome["status"] == "accepted" and st.version == 4 and st.state_hash == "h4"
    assert [x.word for x in st.words][-1] == "moon"


def test_words_per_poem_cap_stops_turns(tmp_path):
    client = FakeClient()
    w, me = _writer(tmp_path, client, PickBrain("moon"))
    w.playable = {"moon": 1}
    w.max_words_per_poem = 1
    st = _state(last="did:key:z6MkX", syllables=1)
    assert w.turn(st) is not None
    assert w.turn(st) is None


def test_run_dry_proposes_once_per_version_and_only_when_allowed(tmp_path):
    ref = identity.create(tmp_path / "ref.pem", "pw")
    setup = json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": "setup-xav", "game_id": "xav",
                        "poem_room": ROOM, "room_generation": 1, "state_hash": "h0", "sender_did": ref.did})
    other = "did:key:z6MkX"
    r1 = json.dumps({"type": "sonnet.receipt.v1", "status": "accepted", "request_id": "o-1", "sender_did": other,
                     "version": 1, "state_hash": "h1", "syllables": 1, "complete": False})
    pages = [
        RoomPage(ROOM, 1, 1, [Message(1, "t", ref.did, setup, 1, ref.sign(ROOM, 1, setup))], generation=1),
        RoomPage(ROOM, 2, 2, [Message(2, "t", ref.did, r1, 2, ref.sign(ROOM, 2, r1))], generation=1),
        RoomPage(ROOM, None, None, [], generation=1),
        RoomPage(ROOM, None, None, [], generation=1),
    ]
    client = FakeClient(pages)
    w, me = _writer(tmp_path, client, PickBrain("moon"))
    w.referee_did = ref.did
    w.playable = {"moon": 1}
    st = poem.PoemState(game_id="xav", room=ROOM, lexicon=LEX)
    w.run(st, max_steps=4, sleep=lambda s: None)
    props = [json.loads(l) for l in (tmp_path / "arc" / f"{ROOM}.jsonl").read_text().splitlines()
             if '"proposal"' in l]
    # v0 (apres setup) puis v1 (apres le mot de l'autre) : une proposition par version, jamais deux
    assert [json.loads(p["text"])["version"] for p in props] == [0, 1]
    assert client.said == []
