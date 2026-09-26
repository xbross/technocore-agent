"""close-1 : vendeur prudent, valide par Xav le 26/09 (vente d'environ 43 contrats, gardee jusqu'au bout).

A chaque balayage : sert d'abord les offres d'achat ouvertes les mieux payees (proches de la reference),
puis propose le reste un peu au-dessus de la reference. Garde-fous : ne fait QUE vendre, ne contresigne
jamais deux fois la meme offre, plafonds de messages par balayage et au total, arret a la cible.

Comptage prudent des executions (l'arbitre tronque parfois ses listes) : un echange que nous postons
complet est compte regle si la liste des rejets de son balayage est complete et ne le cite pas, ou s'il
est cite regle ; compte 'assumed' si cette liste est tronquee ; toute preuve ulterieure l'emporte. Une offre
ouverte n'est comptee que sur preuve positive. Un sous-comptage ne peut que pousser a proposer davantage,
et le controle des fonds de l'arbitre plafonne de toute facon la position a notre capacite."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path

from ..client import ApiError, Duplicate, NetworkError, RateLimited
from . import trade

log = logging.getLogger("technocore.close1.seller")
PRICE_ROOM, FLOW_ROOM, TRADE_ROOM = "d-close1-price", "d-close1-flow", "close1"
SWEEP_S = 300


@dataclass(frozen=True)
class Sweep:
    n: int          # dernier balayage publie
    ref: Decimal    # reference de ce balayage
    lo: Decimal     # limites valables au balayage suivant
    hi: Decimal


@dataclass(frozen=True)
class Bid:
    id: str
    terms: dict
    maker_sig: str
    px: Decimal
    qty: Decimal


def parse_price(text: str) -> Sweep | None:
    try:
        d = json.loads(text)
        if not isinstance(d, dict) or d.get("t") != "price":
            return None
        return Sweep(int(d["n"]), Decimal(str(d["ref"]["px"])), Decimal(str(d["limits"][0])), Decimal(str(d["limits"][1])))
    except (ValueError, KeyError, TypeError, IndexError, ArithmeticError):
        return None


def parse_bid(msg, me: str, sweep: Sweep, min_ratio: Decimal) -> Bid | None:
    """Offre d'achat ouverte, fraiche, dans les limites, pas trop sous la reference, signee par son emetteur."""
    if not msg.signed:
        return None
    try:
        d = json.loads(msg.text)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("t") != "trade" or d.get("season") != trade.SEASON or "taker_sig" in d:
        return None
    t, sig = d.get("terms"), d.get("maker_sig")
    if not isinstance(t, dict) or set(t) != trade.KEYS or not isinstance(sig, str):
        return None
    if t["side"] != "buy" or t["taker"] != "any" or t["maker"] == me:
        return None
    if not isinstance(t["id"], str) or not trade.TRADE_ID.fullmatch(t["id"]):
        return None
    if not isinstance(t["maker"], str) or not trade.DID.fullmatch(t["maker"]):
        return None
    qty, px = trade.amount(t["qty"]), trade.amount(t["px"])
    if qty is None or px is None or qty < trade.MIN_QTY:
        return None
    if type(t["until"]) is not int or t["until"] < sweep.n + 1:
        return None
    if not (sweep.lo <= px <= sweep.hi) or px < sweep.ref * min_ratio:
        return None
    if not trade.verify_maker(t, sig):
        return None
    return Bid(t["id"], t, sig, px, qty)


def pick_bids(bids: list[Bid], remaining: Decimal, max_count: int) -> list[Bid]:
    """Meilleurs prix d'abord ; jamais plus que la quantite restante (une offre se prend entiere)."""
    out, seen = [], set()
    for b in sorted(bids, key=lambda b: (-b.px, -b.qty, b.id)):
        if len(out) >= max_count:
            break
        if b.id in seen or b.qty > remaining:
            continue
        out.append(b)
        seen.add(b.id)
        remaining -= b.qty
    return out


class Ledger:
    """Carnet local : nos echanges, leur statut, et les compteurs de messages (plafonds)."""

    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text("utf-8"))
        else:
            self.data = {"trades": {}, "posts": 0, "posts_by_sweep": {}, "flow_cursor": None, "flow_n": 0}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), "utf-8")
        tmp.replace(self.path)

    def add_taken(self, tid: str, qty: Decimal, px: Decimal, applies: int) -> None:
        self.data["trades"][tid] = {"kind": "taken", "qty": str(qty), "px": str(px), "applies": applies, "status": "pending"}
        self.save()

    def add_offer(self, tid: str, qty: Decimal, px: Decimal, until: int) -> None:
        self.data["trades"][tid] = {"kind": "offer", "qty": str(qty), "px": str(px), "until": until, "status": "open"}
        self.save()

    def note_post(self, n: int) -> None:
        self.data["posts"] += 1
        k = str(n)
        self.data["posts_by_sweep"][k] = self.data["posts_by_sweep"].get(k, 0) + 1
        self.save()

    def posts_in(self, n: int) -> int:
        return self.data["posts_by_sweep"].get(str(n), 0)

    def apply_flow(self, flow: dict) -> None:
        n = int(flow.get("n", 0))
        settled = {x for x in flow.get("settled", []) if isinstance(x, str)} if isinstance(flow.get("settled"), list) else set()
        void = ({v[0]: v[1] for v in flow["void"] if isinstance(v, list) and len(v) >= 2}
                if isinstance(flow.get("void"), list) else {})
        void_complete = isinstance(flow.get("void"), list) and "void" not in (flow.get("omitted") or {})
        for tid, rec in self.data["trades"].items():
            if tid in void:
                rec["status"], rec["reason"], rec["sweep"] = "void", str(void[tid]), n
            elif tid in settled:
                rec["status"], rec["sweep"] = "settled", n
            elif rec["kind"] == "taken" and rec["status"] == "pending" and rec["applies"] <= n:
                rec["status"], rec["sweep"] = ("settled" if void_complete else "assumed"), n
        self.data["flow_n"] = max(self.data.get("flow_n", 0), n)
        self.save()

    def expire_offers(self, last_sweep: int) -> None:
        for rec in self.data["trades"].values():
            if rec["kind"] == "offer" and rec["status"] == "open" and rec["until"] <= last_sweep:
                rec["status"] = "expired"
        self.save()

    def _sum(self, pred) -> Decimal:
        return sum((Decimal(r["qty"]) for r in self.data["trades"].values() if pred(r)), Decimal(0))

    def filled(self) -> Decimal:
        return self._sum(lambda r: r["status"] in ("settled", "assumed"))

    def pending_qty(self) -> Decimal:
        return self._sum(lambda r: r["kind"] == "taken" and r["status"] == "pending")

    def open_offer_qty(self) -> Decimal:
        return self._sum(lambda r: r["kind"] == "offer" and r["status"] == "open")

    def summary(self) -> dict:
        by = {}
        for r in self.data["trades"].values():
            k = f"{r['kind']}:{r['status']}"
            by[k] = str(Decimal(by.get(k, "0")) + Decimal(r["qty"]))
        return {"filled": str(self.filled()), "pending": str(self.pending_qty()), "open_offers": str(self.open_offer_qty()),
                "posts": self.data["posts"], "by_status_qty": by}


class Seller:
    def __init__(self, client, ident, referee_did: str, ledger_path, target: Decimal, dry_run: bool = True,
                 max_posts_per_sweep: int = 8, max_posts_total: int = 300, min_ratio: Decimal = Decimal("0.99"),
                 premium: Decimal = Decimal("0.005"), offer_chunk: Decimal = Decimal("11"), lock_sweep: int = 2556,
                 guard_s: float = 20.0, now=time.time, sleep=time.sleep):
        self.client, self.ident, self.referee = client, ident, referee_did
        self.me = ident.did
        self.target, self.dry_run = Decimal(target), dry_run
        self.max_posts_per_sweep, self.max_posts_total = max_posts_per_sweep, max_posts_total
        self.min_ratio, self.premium, self.offer_chunk = min_ratio, premium, offer_chunk
        self.lock_sweep, self.guard_s, self.now, self.sleep = lock_sweep, guard_s, now, sleep
        self.ledger = Ledger(ledger_path)
        self._counter = 0
        self._last_nonce = 0

    # --- lectures (seul l'arbitre fait foi) ---
    def _latest_sweep(self) -> Sweep | None:
        page = self.client.read(PRICE_ROOM, limit=5)
        for m in sorted(page.messages, key=lambda m: m.seq, reverse=True):
            if m.signed and m.sender == self.referee:
                sw = parse_price(m.text)
                if sw is not None:
                    return sw
        return None

    def _ingest_flow(self) -> None:
        cur = self.ledger.data.get("flow_cursor")
        page = self.client.read(FLOW_ROOM, since=cur, limit=50) if cur else self.client.read(FLOW_ROOM, limit=20)
        for m in sorted(page.messages, key=lambda m: m.seq):
            if m.signed and m.sender == self.referee:
                try:
                    d = json.loads(m.text)
                except ValueError:
                    d = None
                if isinstance(d, dict) and d.get("t") == "flow":
                    self.ledger.apply_flow(d)
            self.ledger.data["flow_cursor"] = max(self.ledger.data.get("flow_cursor") or 0, m.seq)
        self.ledger.save()

    # --- ecritures ---
    def _guard_sell(self, text: str) -> None:
        """Garde-fou : nous ne faisons QUE vendre."""
        d = json.loads(text)
        t = d["terms"]
        ours_as_maker = t["maker"] == self.me and t["side"] == "sell" and "taker_sig" not in d
        ours_as_taker = t["maker"] != self.me and t["side"] == "buy" and d.get("taker") == self.me and "taker_sig" in d
        if not (ours_as_maker or ours_as_taker):
            raise RuntimeError(f"garde-fou : message qui ne serait pas une vente ({t.get('id')})")

    def _post(self, text: str, n: int) -> None:
        self._guard_sell(text)
        self.ledger.note_post(n)  # compte la tentative avant l'envoi : une boucle d'echecs reste plafonnee
        nonce = max(int(self.now() * 1000), self._last_nonce + 1)
        self._last_nonce = nonce
        self.client.say_signed(self.ident, TRADE_ROOM, text, nonce)

    def _rid(self) -> str:
        self._counter += 1
        return f"xav-s{int(self.now() * 1000)}-{self._counter}"

    def _near_boundary(self) -> bool:
        return SWEEP_S - (self.now() % SWEEP_S) < self.guard_s

    def step(self) -> dict:
        sweep = self._latest_sweep()
        if sweep is None:
            return {"action": "no-price"}
        self._ingest_flow()
        self.ledger.expire_offers(sweep.n)
        filled = self.ledger.filled()
        if filled >= self.target:
            return {"action": "done", "filled": str(filled), "sweep": sweep.n}
        if sweep.n >= self.lock_sweep:
            return {"action": "locked", "filled": str(filled)}
        if self.ledger.data["posts"] >= self.max_posts_total:
            return {"action": "capped", "filled": str(filled)}
        remaining = self.target - filled - self.ledger.pending_qty() - self.ledger.open_offer_qty()
        budget = min(self.max_posts_per_sweep - self.ledger.posts_in(sweep.n), self.max_posts_total - self.ledger.data["posts"])
        if remaining < trade.MIN_QTY or budget <= 0:
            return {"action": "wait", "filled": str(filled), "sweep": sweep.n}
        if self._near_boundary():
            return {"action": "wait", "why": "boundary", "sweep": sweep.n}
        plan = []
        known = set(self.ledger.data["trades"])
        page = self.client.read(TRADE_ROOM, limit=200)
        bids = [b for b in (parse_bid(m, self.me, sweep, self.min_ratio) for m in page.messages) if b and b.id not in known]
        for b in pick_bids(bids, remaining, budget):
            text = trade.trade_text(b.terms, self.me, b.maker_sig, trade.sign_taker(self.ident, b.terms, self.me))
            plan.append({"kind": "take", "id": b.id, "qty": str(b.qty), "px": str(b.px)})
            if not self.dry_run:
                self._post(text, sweep.n)
                self.ledger.add_taken(b.id, b.qty, b.px, applies=sweep.n + 1)
            remaining -= b.qty
            budget -= 1
        # une seule offre ouverte a la fois : le reste de la cible reste disponible pour servir les acheteurs
        if remaining >= trade.MIN_QTY and budget > 0 and self.ledger.open_offer_qty() == 0:
            qty = min(remaining, self.offer_chunk).quantize(trade.CENT, rounding=ROUND_DOWN)
            px = min((sweep.ref * (1 + self.premium)).quantize(trade.CENT, rounding=ROUND_HALF_UP), sweep.hi)
            terms = trade.make_terms(self._rid(), self.me, "sell", qty, px, "any", sweep.n + 1)
            text = trade.offer_text(terms, trade.sign_maker(self.ident, terms))
            plan.append({"kind": "offer", "id": terms["id"], "qty": terms["qty"], "px": terms["px"], "until": terms["until"]})
            if not self.dry_run:
                self._post(text, sweep.n)
                self.ledger.add_offer(terms["id"], qty, px, until=sweep.n + 1)
        if not plan:
            return {"action": "wait", "filled": str(filled), "sweep": sweep.n}
        return {"action": "planned" if self.dry_run else "posted", "sweep": sweep.n, "ref": str(sweep.ref),
                "filled": str(filled), "plan": plan}

    def run(self, stop=None, poll_s: float = 20.0) -> str:
        log.info("vendeur close-1 %s : cible %s contrats, plafonds %d/balayage et %d au total",
                 "DRY-RUN" if self.dry_run else "LIVE", self.target, self.max_posts_per_sweep, self.max_posts_total)
        last_filled = None
        while not (stop is not None and stop.is_set()):
            try:
                out = self.step()
            except (NetworkError, ApiError, RateLimited, Duplicate) as e:
                log.warning("reseau/API : %s", e)
                (stop.wait(60) if stop is not None else self.sleep(60))
                continue
            if out["action"] in ("posted", "planned"):
                log.info("balayage %s ref %s : %s", out["sweep"], out["ref"], json.dumps(out["plan"]))
            if out.get("filled") != last_filled:
                last_filled = out.get("filled")
                log.info("execute (compte prudent) : %s / %s | %s", last_filled, self.target, json.dumps(self.ledger.summary()))
            if out["action"] in ("done", "locked", "capped"):
                log.info("arret : %s", json.dumps(out))
                return out["action"]
            (stop.wait(poll_s) if stop is not None else self.sleep(poll_s))
        return "stopped"
