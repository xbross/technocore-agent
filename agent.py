#!/usr/bin/env python3
"""CLI de l'agent Technocore.

  python agent.py init            cree l'identite (une seule fois)
  python agent.py whoami          affiche le DID, l'empreinte et la note publiee
  python agent.py say ROOM TEXTE  envoie un message signe et verifie qu'il est passe
  python agent.py run             boucle autonome (Ctrl-C pour arreter proprement)
"""

from __future__ import annotations

import argparse
import logging
import sys

from technocore_agent import identity
from technocore_agent.agent import Agent, did_note_paths, setup_logging
from technocore_agent.brain import build_brain
from technocore_agent.client import TechnocoreClient
from technocore_agent.config import Config
from technocore_agent.state import State


def cmd_init(cfg: Config, args) -> int:
    if cfg.key_path.exists():
        print(f"Une identite existe deja: {cfg.key_path}\nRien n'a ete modifie.")
        return 1
    import getpass, os
    p1 = os.environ.get("TECHNOCORE_PASSPHRASE") or getpass.getpass("Choisissez une passphrase (ne s'affiche pas): ")
    if not os.environ.get("TECHNOCORE_PASSPHRASE"):
        p2 = getpass.getpass("Confirmez la passphrase: ")
        if p1 != p2:
            print("Les deux saisies different. Rien n'a ete cree.")
            return 1
    ident = identity.create(cfg.key_path, p1)
    print(f"Cle creee (chiffree, permissions 600): {cfg.key_path}")
    print(f"DID       : {ident.did}")
    print(f"Empreinte : {ident.fingerprint}")
    print("Sauvegardez ce fichier ET la passphrase hors ligne : sans les deux, l'identite est perdue.")
    return 0


def load_identity(cfg: Config) -> identity.Identity:
    return identity.load(cfg.key_path, identity.passphrase_from_env_or_prompt())


def cmd_whoami(cfg: Config, args) -> int:
    ident = load_identity(cfg)
    client = TechnocoreClient(cfg.base_url)
    print(f"DID       : {ident.did}")
    print(f"Empreinte : {ident.fingerprint}")
    for ns, key in did_note_paths(ident.fingerprint):
        print(f"/kv/{ns}/{key} -> {client.kv_get(ns, key)!r}")
    return 0


def cmd_say(cfg: Config, args) -> int:
    setup_logging(cfg)
    ident = load_identity(cfg)
    client = TechnocoreClient(cfg.base_url)
    state = State.load(cfg.state_path)
    agent = Agent(cfg, ident, client, build_brain(cfg.model), state)
    last = client.read(args.room, limit=1).last_seq
    ok = agent.post_and_confirm(args.room, args.text, last)
    state.save()
    print("OK: message signe confirme" if ok else "ECHEC: voir le journal")
    return 0 if ok else 1


def cmd_run(cfg: Config, args) -> int:
    setup_logging(cfg, logging.DEBUG if args.verbose else logging.INFO)
    ident = load_identity(cfg)
    client = TechnocoreClient(cfg.base_url)
    state = State.load(cfg.state_path)
    agent = Agent(cfg, ident, client, build_brain(cfg.model), state)
    agent.run(max_cycles=args.cycles)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("whoami")
    p_say = sub.add_parser("say")
    p_say.add_argument("room")
    p_say.add_argument("text")
    p_run = sub.add_parser("run")
    p_run.add_argument("--cycles", type=int, default=None, help="s'arreter apres N tours (tests)")
    p_run.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    cfg = Config.from_env()
    try:
        return {"init": cmd_init, "whoami": cmd_whoami, "say": cmd_say, "run": cmd_run}[args.cmd](cfg, args)
    except identity.IdentityError as e:
        print(f"Erreur d'identite: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
