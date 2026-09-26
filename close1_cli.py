#!/usr/bin/env python3
"""Concours close-1 : execution de la vente validee par Xav le 26/09, et etat du carnet local.

  sell [--target 43] [--live --yes] [--once]   vend jusqu'a la cible (DRY-RUN par defaut, rien n'est poste)
  status                                      resume du carnet local (aucun appel reseau)
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from technocore_agent import identity
from technocore_agent.client import TechnocoreClient
from technocore_agent.close1 import seller

PROJECT_DIR = Path(__file__).resolve().parent
LEDGER = PROJECT_DIR / "state" / "close1_seller.json"
KEY = PROJECT_DIR / "identity" / "agent_key.pem"
REFEREE = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"  # seed close-1 du 25/09, meme arbitre que sonnet-2


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    path = PROJECT_DIR / "logs" / "close1.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    for h in (RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sell")
    s.add_argument("--target", default="43")
    s.add_argument("--live", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--once", action="store_true")
    s.add_argument("--poll", type=float, default=6.0)
    sub.add_parser("status")
    args = p.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(seller.Ledger(LEDGER).summary(), indent=1))
        return 0
    if args.live and not args.yes:
        print("erreur: la vente signe des messages en ton nom ; relance avec --live --yes apres validation", file=sys.stderr)
        return 2
    _setup_logging()
    ident = identity.load(KEY, identity.passphrase_from_env_or_prompt())
    sl = seller.Seller(TechnocoreClient(), ident, REFEREE, LEDGER, target=Decimal(args.target), dry_run=not args.live)
    if args.once:
        print(json.dumps(sl.step(), indent=1))
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    out = sl.run(stop, poll_s=args.poll)
    return 0 if out in ("done", "locked", "capped", "stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
