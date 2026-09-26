"""Concours close-1 : signatures d'echange, lecture des offres, comptage prudent des executions, vendeur."""
import json
from decimal import Decimal as D

import pytest

from technocore_agent import identity
from technocore_agent.client import Message, RoomPage, SayResult
from technocore_agent.close1 import seller, trade

PRICE, FLOW, ROOM = "d-close1-price", "d-close1-flow", "close1"


@pytest.fixture
def ids(tmp_path):
    return {k: identity.create(tmp_path / f"{k}.pem", "pw") for k in ("ref", "me", "bob", "eve")}


def _terms(ident, side="buy", qty="5", px="224.50", until=11, tid="b1", taker="any"):
    return trade.make_terms(tid, ident.did, side, D(qty), D(px), taker, until)


def _offer(ident, **kw):
    t = _terms(ident, **kw)
    return trade.offer_text(t, trade.sign_maker(ident, t))


def test_terms_are_sorted_compact_and_both_signatures_verify(ids):
    t = _terms(ids["bob"], side="sell", taker=ids["me"].did)
    assert trade.terms_json(t) == json.dumps(t, sort_keys=True, separators=(",", ":"))
    ms, ts = trade.sign_maker(ids["bob"], t), trade.sign_taker(ids["me"], t, ids["me"].did)
    assert len(ms) == len(ts) == 86 and trade.verify_maker(t, ms) and trade.verify_taker(t, ids["me"].did, ts)
    assert not trade.verify_maker(t, ts)
    full = json.loads(trade.trade_text(t, ids["me"].did, ms, ts))
    assert full == {"t": "trade", "season": "close-1", "terms": t, "taker": ids["me"].did, "maker_sig": ms, "taker_sig": ts}
    assert json.loads(trade.offer_text(t, ms))["taker"] == "any"


@pytest.mark.parametrize("kw", [dict(qty="0.09"), dict(px="224.505"), dict(tid="bad id!"), dict(side="hold")])
def test_make_terms_rejects_shapes_the_referee_would_void(ids, kw):
    with pytest.raises(ValueError):
        _terms(ids["bob"], **kw)


def test_parse_bid_keeps_only_fresh_valid_buy_offers_near_the_reference(ids):
    sw = seller.Sweep(n=10, ref=D("225"), lo=D("213.75"), hi=D("236.25"))
    ok = lambda text, who="bob": seller.parse_bid(Message(1, "t", ids[who].did, text, 1, "s"), ids["me"].did, sw, D("0.995"))
    assert ok(_offer(ids["bob"])).qty == D("5")
    assert ok(_offer(ids["bob"], until=10)) is None               # expire avant le prochain balayage
    assert ok(_offer(ids["bob"], px="237.00")) is None            # hors limites
    assert ok(_offer(ids["bob"], px="223.00")) is None            # trop loin sous la reference
    assert ok(_offer(ids["bob"], side="sell")) is None            # nous ne faisons que vendre
    assert ok(_offer(ids["bob"], taker=ids["eve"].did)) is None   # reserve a quelqu'un d'autre
    assert ok(_offer(ids["me"]), who="me") is None                # notre propre offre
    t = _terms(ids["bob"])
    assert ok(trade.offer_text(t, trade.sign_maker(ids["eve"], t))) is None   # signature d'emetteur fausse
    full = trade.trade_text(t, ids["eve"].did, trade.sign_maker(ids["bob"], t), trade.sign_taker(ids["eve"], t, ids["eve"].did))
    assert ok(full) is None                                       # deja contresignee


def test_pick_bids_prefers_the_best_price_and_never_exceeds_the_remaining_quantity(ids):
    sw = seller.Sweep(n=10, ref=D("225"), lo=D("213.75"), hi=D("236.25"))
    mk = lambda tid, px, qty: seller.parse_bid(Message(1, "t", ids["bob"].did, _offer(ids["bob"], tid=tid, px=px, qty=qty), 1, "s"),
                                                 ids["me"].did, sw, D("0.995"))
    bids = [mk("a", "224.00", "3"), mk("b", "224.90", "8"), mk("c", "224.60", "4"), mk("d", "224.70", "2")]
    assert [b.id for b in seller.pick_bids(bids, D("10"), 5)] == ["b", "d"]
    assert [b.id for b in seller.pick_bids(bids, D("20"), 2)] == ["b", "d"]


def test_ledger_counts_executions_prudently(tmp_path):
    led = seller.Ledger(tmp_path / "l.json")
    led.add_taken("t1", D("5"), D("224.5"), applies=11)
    led.add_taken("t2", D("3"), D("224.4"), applies=11)
    led.add_taken("t3", D("2"), D("224.3"), applies=12)
    led.add_offer("o1", D("7"), D("226.13"), until=11)
    led.add_offer("o2", D("6"), D("226.20"), until=11)
    led.apply_flow({"n": 11, "settled": ["o1"], "void": [["t2", "funds"]]})    # liste des rejets complete
    led.apply_flow({"n": 12, "settled": [], "void": [], "omitted": {"void": 40}})   # rejets tronques
    led.expire_offers(12)
    st = {k: v["status"] for k, v in led.data["trades"].items()}
    assert st == {"t1": "settled", "t2": "void", "t3": "assumed", "o1": "settled", "o2": "expired"}
    assert led.filled() == D("14")                                 # t1 + t3 (prudence) + o1
    led.apply_flow({"n": 13, "void": [["t3", "expired"]]})           # une preuve tardive l'emporte
    assert led.data["trades"]["t3"]["status"] == "void" and led.filled() == D("12")
    assert seller.Ledger(tmp_path / "l.json").filled() == D("12")   # persiste


class FakeClient:
    def __init__(self, ids):
        self.ids, self.rooms, self.said = ids, {PRICE: [], FLOW: [], ROOM: []}, []

    def post(self, room, who, text):
        seq = len(self.rooms[room]) + 1
        self.rooms[room].append(Message(seq, "t", self.ids[who].did, text, seq, "s"))

    def price(self, n, ref="225.00", lo="213.75", hi="236.25"):
        self.post(PRICE, "ref", json.dumps({"t": "price", "n": n, "ref": {"px": ref}, "limits": [lo, hi]}))

    def read(self, room, since=None, limit=200, wait=None):
        msgs = [m for m in self.rooms[room] if since is None or m.seq > since][-limit:]
        return RoomPage(room, msgs[0].seq if msgs else None, msgs[-1].seq if msgs else None, msgs, generation=1)

    def say_signed(self, ident, room, text, nonce):
        self.said.append(json.loads(text))
        self.rooms[room].append(Message(len(self.rooms[room]) + 1, "t", ident.did, text, nonce, "s"))
        return SayResult("", len(self.rooms[room]), True)


def _seller(ids, tmp_path, client, **kw):
    kw = {"target": D("12"), "dry_run": False, **kw}
    return seller.Seller(client, ids["me"], ids["ref"].did, tmp_path / "led.json", now=lambda: 1_000_010.0, **kw)


def test_step_takes_good_bids_then_offers_the_rest_above_the_reference_and_never_buys(ids, tmp_path):
    c = FakeClient(ids)
    c.price(10)
    c.post(ROOM, "bob", _offer(ids["bob"], tid="good", px="224.50", qty="5"))
    c.post(ROOM, "bob", _offer(ids["bob"], tid="cheap", px="220.00", qty="5"))
    c.post(ROOM, "bob", _offer(ids["bob"], tid="old", until=9))
    s = _seller(ids, tmp_path, c)
    out = s.step()
    assert out["action"] == "posted"
    taken, offer = c.said
    assert taken["terms"]["id"] == "good" and taken["terms"]["side"] == "buy" and taken["taker"] == ids["me"].did
    assert trade.verify_taker(taken["terms"], ids["me"].did, taken["taker_sig"])
    assert offer["terms"]["side"] == "sell" and offer["terms"]["maker"] == ids["me"].did and "taker_sig" not in offer
    assert D(offer["terms"]["qty"]) == D("7") and D(offer["terms"]["px"]) == D("226.13") and offer["terms"]["until"] == 11
    assert s.step()["action"] == "wait" and len(c.said) == 2        # meme balayage : rien de plus
    for m in c.said:                                                 # garde-fou : nous vendons toujours
        side, maker = m["terms"]["side"], m["terms"]["maker"]
        assert (maker == ids["me"].did and side == "sell") or (maker != ids["me"].did and side == "buy")


def test_step_stops_at_the_target_and_under_the_post_caps(ids, tmp_path):
    c = FakeClient(ids)
    c.price(10)
    s = _seller(ids, tmp_path, c, target=D("5"))
    s.ledger.add_taken("x", D("5"), D("224"), applies=10)
    s.ledger.data["trades"]["x"]["status"] = "settled"
    assert s.step()["action"] == "done" and c.said == []
    s2 = _seller(ids, tmp_path / "b", c, max_posts_total=0)
    (tmp_path / "b").mkdir()
    assert s2.step()["action"] == "capped" and c.said == []


def test_dry_run_posts_nothing(ids, tmp_path):
    c = FakeClient(ids)
    c.price(10)
    c.post(ROOM, "bob", _offer(ids["bob"], tid="good", px="224.50", qty="5"))
    out = _seller(ids, tmp_path, c, dry_run=True).step()
    assert out["action"] == "planned" and len(out["plan"]) == 2 and c.said == []


def test_only_one_open_offer_at_a_time_so_bids_can_still_be_served(ids, tmp_path):
    """26/09 en production : le vendeur empilait 4 offres dans le meme balayage et engageait toute la cible,
    ce qui l'empechait de servir les offres d'achat arrivant ensuite."""
    c = FakeClient(ids)
    c.price(10)
    s = _seller(ids, tmp_path, c, target=D("43"))
    assert s.step()["action"] == "posted" and len(c.said) == 1          # une offre de 11
    assert s.step()["action"] == "wait" and len(c.said) == 1            # pas de deuxieme offre
    c.post(ROOM, "bob", _offer(ids["bob"], tid="late", px="224.80", qty="3"))
    out = s.step()                                                      # mais une offre d'achat reste servie
    assert out["action"] == "posted" and [p["kind"] for p in out["plan"]] == ["take"]
    assert c.said[-1]["terms"]["id"] == "late"


def test_offer_price_steps_down_to_the_reference_while_nothing_fills_and_resets_after_a_fill(ids, tmp_path):
    """Repli valide par Xav : '+1 % puis au prix de reference si personne ne prend'. -0,1 % par balayage sans
    execution, jamais sous la reference ; retour a +0,5 % des qu'une vente passe."""
    c = FakeClient(ids)
    c.price(20)
    s = _seller(ids, tmp_path, c, target=D("43"))
    s.ledger.data.update({"last_filled": "0", "last_fill_sweep": 17})
    assert D(s.step()["plan"][0]["px"]) == D("225.45")      # +0,5 % - 3 x 0,1 %
    c.price(30)
    assert D(s.step()["plan"][0]["px"]) == D("225.00")      # plancher : la reference
    s.ledger.add_taken("f", D("1"), D("224.9"), applies=30)
    s.ledger.data["trades"]["f"]["status"] = "settled"
    c.price(31)
    assert D(s.step()["plan"][0]["px"]) == D("226.13")      # une vente est passee : retour a +0,5 %
