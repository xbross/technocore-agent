#!/usr/bin/env python3
"""Outils du concours de sonnet (sonnet-2). Lecture seule : aucune commande ici n'ecrit sur technocore.chat.

  alphabet            lettres jouables de notre DID (regle du validateur officiel)
  words               mots CMUdict jouables, syllabes, familles de rimes
  roster DID...       couverture d'un roster candidat (union des alphabets, mots-cles)
  pitch               texte ASCII de recrutement pour la room discovery (a relire avant usage)
  fetch-package       telecharge le paquet officiel au commit epingle et verifie les sha256
  verify-package      verifie les sha256 du paquet local
  watch [--once]      veille + archive JSONL des rooms du concours, recus arbitre verifies
  register --yes      (ECRITURE) inscription au concours ; exige participant.armed = true
                      et la validation humaine explicite (--yes). Fige le role et le DID.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from technocore_agent import identity
from technocore_agent.client import TechnocoreClient
from technocore_agent.sonnet import lexicon, package, register
from technocore_agent.sonnet.config import ConfigError, SonnetConfig
from technocore_agent.sonnet.watch import SonnetWatcher

PROJECT_DIR = Path(__file__).resolve().parent
log = logging.getLogger("technocore.sonnet")


def _setup_logging(cfg: SonnetConfig, level: int = logging.INFO) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    log_path = cfg.path.parent / "logs" / "sonnet.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)


def _did(cfg: SonnetConfig, args) -> str:
    did = getattr(args, "did", None) or cfg.did
    if not did:
        raise ConfigError("aucun DID : renseigne participant.did dans sonnet.toml ou passe --did")
    return did


def _lexicon(cfg: SonnetConfig):
    package.verify_package(cfg.package_dir, cfg.package_sha256)
    dic = cfg.package_dir / "cmudict.dict"
    return lexicon.read_lexicon(dic), lexicon.read_pronunciations(dic)


def cmd_alphabet(cfg, args) -> int:
    letters = lexicon.did_alphabet(_did(cfg, args))
    print(f"DID      : {_did(cfg, args)}")
    print(f"lettres  : {''.join(sorted(letters))} ({len(letters)})")
    print(f"manquantes: {''.join(sorted(lexicon.ALPHABET - letters))}")
    return 0


def cmd_words(cfg, args) -> int:
    lex, prons = _lexicon(cfg)
    playable = lexicon.playable_words(lex, lexicon.did_alphabet(_did(cfg, args)))
    if args.syllables:
        playable = {w: n for w, n in playable.items() if n == args.syllables}
    fams = lexicon.rhyme_families(playable, prons)
    if args.json:
        print(json.dumps({"playable": dict(sorted(playable.items())),
                          "rhyme_families": {" ".join(k): sorted(v) for k, v in fams.items()}}, indent=1))
        return 0
    by = {}
    for w, n in playable.items():
        by.setdefault(n, []).append(w)
    print(f"{len(playable)} mots jouables sur {len(lex)} (CMUdict epingle)")
    for n in sorted(by):
        print(f"  {n} syllabe(s): {len(by[n])}")
    print(f"mots-outils jouables: {' '.join(w for w in lexicon.KEY_WORDS if w in playable) or 'aucun'}")
    if args.rhymes:
        for key, words in sorted(fams.items(), key=lambda kv: -len(kv[1]))[: args.rhymes]:
            print(f"  rime -{' '.join(key)}: {' '.join(sorted(words)[:15])}" + (" ..." if len(words) > 15 else ""))
    return 0


def cmd_roster(cfg, args) -> int:
    lex, _ = _lexicon(cfg)
    key = {w: lex[w] for w in lexicon.KEY_WORDS if w in lex}
    cov = lexicon.roster_coverage(args.dids, key)
    print(f"roster de {len(args.dids)} DID : {len(cov.letters)} lettres couvertes")
    print(f"missing letters : {''.join(sorted(cov.missing)) or '-'}")
    print(f"key words ok    : {' '.join(cov.key_words_available)}")
    print(f"key words KO    : {' '.join(cov.key_words_missing) or '-'}")
    for d in args.dids:
        a = lexicon.did_alphabet(d)
        print(f"  {d[-8:]}: {len(a)} lettres, apporte seul: {''.join(sorted(a - set().union(*(lexicon.did_alphabet(o) for o in args.dids if o != d)))) or '-'}")
    return 0


def cmd_pitch(cfg, args) -> int:
    lex, prons = _lexicon(cfg)
    did = _did(cfg, args)
    print(lexicon.pitch(did, lexicon.playable_words(lex, lexicon.did_alphabet(did)), prons))
    return 0


def cmd_fetch(cfg, args) -> int:
    if not cfg.package_commit:
        raise ConfigError("package.commit manquant dans sonnet.toml")
    package.fetch_package(cfg.package_dir, cfg.package_commit, cfg.package_sha256)
    print(f"paquet verifie dans {cfg.package_dir} ({len(cfg.package_sha256)} fichiers, commit {cfg.package_commit[:12]})")
    return 0


def cmd_verify(cfg, args) -> int:
    package.verify_package(cfg.package_dir, cfg.package_sha256)
    print(f"OK: {len(cfg.package_sha256)} fichiers conformes aux sha256 epingles")
    return 0


def _identity(cfg: SonnetConfig) -> identity.Identity:
    key_path = PROJECT_DIR / "identity" / "agent_key.pem"
    return identity.load(key_path, identity.passphrase_from_env_or_prompt())


def cmd_register(cfg, args) -> int:
    if not args.yes:
        print("erreur: l'inscription fige le role et le DID ; relance avec --yes apres validation humaine",
              file=sys.stderr)
        return 2
    _setup_logging(cfg)
    ident = _identity(cfg)
    if cfg.did and cfg.did != ident.did:
        raise ConfigError(f"participant.did ({cfg.did[-8:]}) ne correspond pas a la cle chargee ({ident.did[-8:]})")
    print(f"inscription {cfg.contest_id} role={cfg.role} did=...{ident.did[-8:]} x={cfg.x_account_url}")
    print(f"message: {register.registration_message(cfg, args.request_id)}")
    receipt = register.register(TechnocoreClient(), ident, cfg, args.request_id, wait_seconds=args.wait)
    out = cfg.path.parent / "state" / f"sonnet-registration-{args.request_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if receipt is None:
        print("aucun recu dans le delai ; relancer avec le MEME request_id renvoie le recu d'origine (idempotence)")
        return 1
    out.write_text(json.dumps(receipt, indent=1), "utf-8")
    print(json.dumps(receipt, indent=1))
    print(f"recu archive dans {out}")
    return 0 if receipt.get("status") == "accepted" else 1


def cmd_watch(cfg, args) -> int:
    _setup_logging(cfg, logging.DEBUG if args.verbose else logging.INFO)
    rooms = [cfg.rooms[k] for k in sorted(cfg.rooms)]
    w = SonnetWatcher(TechnocoreClient(), rooms=rooms, referee_did=cfg.referee_did,
                      archive_dir=cfg.archive_dir, state_path=cfg.state_path)
    if args.once:
        counts = w.poll_once()
        print(json.dumps({"archived": counts, "receipts": w.receipts_seen}))
        return 0
    stop = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stop.set())
    w.run(cfg.poll_seconds, stop)
    log.info("veille arretee proprement")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(PROJECT_DIR / "sonnet.toml"))
    parser.add_argument("--did", help="DID a analyser (defaut: participant.did du fichier de config)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("alphabet")
    p = sub.add_parser("words")
    p.add_argument("--json", action="store_true")
    p.add_argument("--syllables", type=int, default=0)
    p.add_argument("--rhymes", type=int, default=0, help="afficher les N plus grandes familles de rimes")
    p = sub.add_parser("roster")
    p.add_argument("dids", nargs="+")
    sub.add_parser("pitch")
    sub.add_parser("fetch-package")
    sub.add_parser("verify-package")
    p = sub.add_parser("watch")
    p.add_argument("--once", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("register")
    p.add_argument("--yes", action="store_true", help="validation humaine explicite")
    p.add_argument("--request-id", default="register-1")
    p.add_argument("--wait", type=float, default=600, help="attente max du recu arbitre (s)")
    args = parser.parse_args(argv)
    handlers = {"alphabet": cmd_alphabet, "words": cmd_words, "roster": cmd_roster, "pitch": cmd_pitch,
                "fetch-package": cmd_fetch, "verify-package": cmd_verify, "watch": cmd_watch,
                "register": cmd_register}
    try:
        cfg = SonnetConfig.load(Path(args.config))
        return handlers[args.cmd](cfg, args)
    except (ConfigError, package.PackageError, register.Disarmed, identity.IdentityError, ValueError) as e:
        print(f"erreur: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
