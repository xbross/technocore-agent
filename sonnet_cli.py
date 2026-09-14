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
  poem-state GAME_ID  reconstruit l'etat d'un poeme depuis les recus arbitre de sa room (lecture)
  play GAME_ID        joue les mots : dry-run par defaut ; --live --yes pour poster (armed requis)
  submit-prep GAME_ID texte canonique, sha256 et paquet de soumission (sans x_post_ids) - lecture
  manage GAME_ID      gestionnaire de roster (lead) : remplace les sieges qui ne signent pas ;
                      dry-run par defaut, --live --yes pour ecrire (retrait, re-signature, invitation)
  countersign GAME_ID --lead DID   contre-signe automatiquement le roster de CE lead pour CE jeu
                      s'il nous nomme (4-8 membres) ; dry-run par defaut, --live --yes pour ecrire
  team-request/roster-sign/withdraw/say  (ECRITURE, --yes + armed) messages de formation d'equipe

Toute ecriture exige participant.armed = true dans sonnet.toml ET --yes sur la ligne de commande.
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
from technocore_agent.sonnet import countersign, lexicon, manager, package, poem, register, team, writer
from technocore_agent.sonnet.roster import roster_status, wait_for_roster
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


def _require_write(cfg: SonnetConfig, args, what: str) -> None:
    if not getattr(args, "yes", False):
        print(f"erreur: {what} est une ecriture signee ; relance avec --yes apres validation humaine", file=sys.stderr)
        raise SystemExit(2)
    if not cfg.armed:
        raise register.Disarmed("participant.armed = false dans sonnet.toml : aucune ecriture autorisee")


def _read_all(client, room: str):
    """Tout l'historique retenu de la room : export JSONL (la pagination ne rend que les 200
    dernieres lignes), puis complement par lecture depuis le dernier seq."""
    if hasattr(client, "export"):
        msgs, gen = client.export(room)
    else:
        msgs, gen = [], None
    since = max((m.seq for m in msgs), default=0)
    while True:
        page = client.read(room, since=since)
        gen = page.generation if page.generation is not None else gen
        if not page.messages:
            return msgs, gen
        msgs.extend(page.messages)
        since = max(m.seq for m in page.messages)


def _team_room(cfg: SonnetConfig, game_id: str) -> str:
    return f"d-sonnet-2-team-{team.check_game_id(game_id)}"


def _state_for(cfg: SonnetConfig, client, game_id: str):
    lex, prons = _lexicon(cfg)
    room = _team_room(cfg, game_id)
    msgs, gen = _read_all(client, room)
    st = poem.PoemState.from_messages(msgs, room, cfg.referee_did, game_id, lex)
    if st.generation is None:
        st.generation = gen
    return st, lex, prons


def _print_state(st) -> None:
    print(f"{st.game_id}: version {st.version}, state_hash {st.state_hash}, {st.syllables}/140 syllabes, "
          f"complet={st.complete}, generation {st.generation}, dernier contributeur ...{(st.last_contributor or '-')[-8:]}")
    for i, line in enumerate(st.lines(), 1):
        print(f"  {i:2d}. {' '.join(line)}")
    if not st.complete:
        print(f"  ligne {st.line_index + 1} : {st.remaining} syllabe(s) restante(s)")


def cmd_poem_state(cfg, args) -> int:
    st, _, _ = _state_for(cfg, TechnocoreClient(), args.game_id)
    _print_state(st)
    return 0


def cmd_roster_status(cfg, args) -> int:
    client = TechnocoreClient()
    msgs, _ = _read_all(client, cfg.rooms["discovery"])
    st = roster_status(msgs, cfg.rooms["discovery"], cfg.referee_did, args.game_id, cfg.did or "")
    if args.json:
        print(json.dumps(st))
    else:
        print(f"{st['game_id']}: liste seq {st['roster_seq']}, {len(st['signed'])}/{len(st['members'])} consentements acceptes, ready={st['ready']}")
        for m in st["members"]:
            print(f"  {'OK ' if m in st['signed'] else '.. '}{m}{'  <- nous' if m == cfg.did else ''}")
    return 0


def cmd_submit_prep(cfg, args) -> int:
    st, lex, _ = _state_for(cfg, TechnocoreClient(), args.game_id)
    _print_state(st)
    if not st.complete:
        print("poeme incomplet : pas de soumission possible", file=sys.stderr)
        return 1
    text = poem.canonical_text(st.lines())
    validator = package.load_validator(cfg.package_dir, cfg.package_sha256)
    counts = validator.validate_poem(text, lex, exact_ten=True)
    digest = poem.poem_sha256(text)
    print("\n--- texte canonique (a publier tel quel depuis le compte X du dernier contributeur) ---")
    print(text)
    print(f"--- sha256 {digest} ; syllabes par ligne {counts}")
    packet = {"type": "sonnet.submit.v1", "contest_id": cfg.contest_id, "game_id": st.game_id,
              "poem_room": st.room, "room_generation": st.generation, "final_version": st.version,
              "poem_sha256": digest, "x_post_ids": ["<a completer apres publication>"],
              "request_id": f"submit-{st.game_id}-1"}
    print("paquet (x_post_ids a completer) :", json.dumps(packet, separators=(",", ":")))
    who = st.last_contributor or ""
    print("dernier contributeur :", who, "(c'est nous)" if cfg.did == who else "(pas nous : il publie et soumet)")
    return 0


def _brain(cfg: SonnetConfig):
    from technocore_agent.brain import ClaudeCliBrain
    import os
    binary = os.environ.get("TECHNOCORE_CLAUDE_BIN", "claude")
    cli = ClaudeCliBrain(binary=binary, model=cfg.model, effort=cfg.effort, max_thinking_tokens=None,
                         max_calls_per_hour=int(cfg.extra.get("brain", {}).get("max_calls_per_hour", 40)),
                         cwd=str(PROJECT_DIR))
    return writer.ClaudeWordBrain(cli)


def cmd_play(cfg, args) -> int:
    live = bool(args.live)
    if live:
        _require_write(cfg, args, "jouer des mots")
    _setup_logging(cfg, logging.DEBUG if args.verbose else logging.INFO)
    ident = _identity(cfg)
    client = TechnocoreClient()
    stop = threading.Event()
    for s_ in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s_, lambda *_: stop.set())
    if args.wait_roster:
        def status():
            msgs, _ = _read_all(client, cfg.rooms["discovery"])
            return roster_status(msgs, cfg.rooms["discovery"], cfg.referee_did, args.game_id, ident.did)
        log.info("%s: attente du roster_ready de l'arbitre avant de jouer", args.game_id)
        if wait_for_roster(status, poll_seconds=30, stop=stop) is None:
            return 0
        log.info("%s: roster pret, le jeu peut commencer", args.game_id)
    st, lex, prons = _state_for(cfg, client, args.game_id)
    _print_state(st)
    playable = lexicon.playable_words(lex, lexicon.did_alphabet(ident.did))
    validator = package.load_validator(cfg.package_dir, cfg.package_sha256)
    brain = writer.HeuristicBrain() if args.no_model else _brain(cfg)
    w = writer.Writer(client, ident, cfg.contest_id, cfg.referee_did, playable, prons, brain,
                      archive_dir=cfg.archive_dir, dry_run=not live,
                      max_words_per_poem=int(cfg.extra.get("brain", {}).get("max_words_per_poem", 60)),
                      official_validate=lambda word, did: validator.validate_word(word, did, lex))
    log.info("%s: mode %s, modele %s", args.game_id, "LIVE" if live else "DRY-RUN", "aucun" if args.no_model else cfg.model)
    w.run(st, stop=stop, max_steps=args.steps)
    _print_state(st)
    return 0


def cmd_manage(cfg, args) -> int:
    live = bool(args.live)
    if live:
        _require_write(cfg, args, "gerer le roster")
    _setup_logging(cfg, logging.DEBUG if args.verbose else logging.INFO)
    ident = _identity(cfg)
    client = TechnocoreClient()
    lex, _ = _lexicon(cfg)
    key = {w: lex[w] for w in lexicon.KEY_WORDS if w in lex}
    room = _team_room(cfg, args.game_id)
    page = client.read(room, limit=1)
    generation = page.generation if page.generation is not None else 1
    rules = manager.Rules(patience_s=args.patience_min * 60, active_window_s=args.active_min * 60,
                          max_replacements=args.max_replacements)
    from technocore_agent.sonnet.watch import Archive
    m = manager.RosterManager(client, ident, cfg.referee_did, cfg.contest_id, args.game_id, cfg.rooms["discovery"],
                              room, generation, key, state_path=cfg.state_path.parent / f"sonnet_manager_{args.game_id}.json",
                              rules=rules, dry_run=not live, archive=Archive(cfg.archive_dir),
                              hold_path=cfg.state_path.parent / "sonnet_hold.json")
    log.info("%s: gestionnaire de roster en mode %s (patience %d min, fenetre d'activite %d min, generation %s)",
             args.game_id, "LIVE" if live else "DRY-RUN", args.patience_min, args.active_min, generation)
    if args.once:
        print(json.dumps(m.run_once(), indent=1))
        return 0
    stop = threading.Event()
    for s_ in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s_, lambda *_: stop.set())
    m.run(args.poll, stop)
    return 0


def cmd_countersign(cfg, args) -> int:
    live = bool(args.live)
    if live:
        _require_write(cfg, args, "la contre-signature automatique")
    leads = set()
    for item in args.lead:
        for d in item.split(","):
            d = d.strip()
            if d and not lexicon.ED25519_DID.fullmatch(d):
                raise ConfigError(f"--lead: DID did:key:z6Mk... attendu ({d[:20]}...)")
            if d:
                leads.add(d)
    if not leads:
        raise ConfigError("--lead: au moins un DID")
    _setup_logging(cfg, logging.DEBUG if args.verbose else logging.INFO)
    ident = _identity(cfg)
    from technocore_agent.sonnet.watch import Archive
    game = None if args.game_id == "any" else team.check_game_id(args.game_id)
    cs = countersign.CounterSigner(TechnocoreClient(), ident, cfg.referee_did, cfg.contest_id, game,
                                   lead_dids=leads, discovery_room=cfg.rooms["discovery"], dry_run=not live,
                                   archive=Archive(cfg.archive_dir), release_game=args.release_game,
                                   hold_path=cfg.state_path.parent / "sonnet_hold.json")
    # depart : on ne relit pas tout l'historique, seulement ce qui arrive apres le lancement
    page = TechnocoreClient().read(cfg.rooms["discovery"], limit=1)
    cs.cursor = int(page.last_seq or 0) - int(args.lookback)
    if args.once:
        print(json.dumps(cs.step(), indent=1))
        return 0
    stop = threading.Event()
    for s_ in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s_, lambda *_: stop.set())
    cs.run(stop)
    return 0


def _post(cfg, ident, room: str, text: str) -> int:
    client = TechnocoreClient()
    res = client.say_signed(ident, room, text, int(__import__("time").time() * 1000))
    print(f"poste dans {room}: seq={res.seq} verifie={res.verified}")
    print(text)
    return 0 if res.verified else 1


def cmd_team_request(cfg, args) -> int:
    _require_write(cfg, args, "la demande de room")
    text = team.team_request_message(cfg.contest_id, args.game_id, args.request_id or f"room-{args.game_id}-1")
    if args.dry_run:
        print(text)
        return 0
    return _post(cfg, _identity(cfg), cfg.rooms["discovery"], text)


def cmd_roster_sign(cfg, args) -> int:
    _require_write(cfg, args, "la signature du roster")
    text = team.roster_message(cfg.contest_id, args.game_id, _team_room(cfg, args.game_id), args.generation,
                               args.members, args.request_id or f"roster-{args.game_id}-1")
    if cfg.did and cfg.did not in args.members:
        raise ConfigError("notre DID n'est pas dans la liste des membres")
    if args.dry_run:
        print(text)
        return 0
    return _post(cfg, _identity(cfg), cfg.rooms["discovery"], text)


def cmd_withdraw(cfg, args) -> int:
    _require_write(cfg, args, "le retrait")
    text = team.withdraw_message(cfg.contest_id, args.game_id, args.request_id or f"wd-{args.game_id}-1")
    if args.dry_run:
        print(text)
        return 0
    return _post(cfg, _identity(cfg), cfg.rooms["discovery"], text)


def cmd_say(cfg, args) -> int:
    _require_write(cfg, args, "un message dans une room du concours")
    from technocore_agent.safety import check_reply
    room = cfg.rooms.get(args.room, args.room)
    if not args.text.isascii():
        raise ValueError("texte ASCII uniquement")
    # notre propre URL X enregistree est la seule exception au filtre anti-liens (exigee par le concours)
    probe = args.text.replace(cfg.x_account_url, "OUR-REGISTERED-X-ACCOUNT") if cfg.x_account_url else args.text
    reason = check_reply(probe)
    if reason:
        raise ValueError(f"filtre de sortie: {reason}")
    if args.dry_run:
        print(f"[{room}] {args.text}")
        return 0
    return _post(cfg, _identity(cfg), room, args.text)


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
    p = sub.add_parser("poem-state")
    p.add_argument("game_id")
    p = sub.add_parser("submit-prep")
    p.add_argument("game_id")
    p = sub.add_parser("roster-status")
    p.add_argument("game_id")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("play")
    p.add_argument("game_id")
    p.add_argument("--live", action="store_true", help="poster reellement (sinon dry-run)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-model", action="store_true", help="choix heuristique sans appel de modele")
    p.add_argument("--steps", type=int, default=None, help="nombre de lectures max (defaut: infini)")
    p.add_argument("--wait-roster", action="store_true", help="attendre le recu roster_ready de l'arbitre avant de jouer")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("manage")
    p.add_argument("game_id")
    p.add_argument("--live", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--patience-min", type=float, default=45)
    p.add_argument("--active-min", type=float, default=90)
    p.add_argument("--max-replacements", type=int, default=12)
    p.add_argument("--poll", type=float, default=60)
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("countersign")
    p.add_argument("game_id", help="game_id, ou 'any' pour tout jeu dont la room correspond")
    p.add_argument("--lead", required=True, action="append", help="DID de lead autorise (repetable, ou liste separee par des virgules)")
    p.add_argument("--release-game", default=None, help="notre propre equipe a liberer avant de signer ailleurs")
    p.add_argument("--live", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--lookback", type=int, default=200, help="nb de lignes recentes a relire au demarrage")
    p.add_argument("-v", "--verbose", action="store_true")
    for name in ("team-request", "withdraw"):
        p = sub.add_parser(name)
        p.add_argument("game_id")
        p.add_argument("--yes", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--request-id")
    p = sub.add_parser("roster-sign")
    p.add_argument("game_id")
    p.add_argument("--generation", type=int, required=True)
    p.add_argument("--members", nargs="+", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--request-id")
    p = sub.add_parser("say")
    p.add_argument("room", help="cle de [rooms] (discovery, campaign...) ou nom de room")
    p.add_argument("text")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    handlers = {"alphabet": cmd_alphabet, "words": cmd_words, "roster": cmd_roster, "pitch": cmd_pitch,
                "fetch-package": cmd_fetch, "verify-package": cmd_verify, "watch": cmd_watch,
                "register": cmd_register, "poem-state": cmd_poem_state, "submit-prep": cmd_submit_prep,
                "roster-status": cmd_roster_status,
                "play": cmd_play, "team-request": cmd_team_request, "roster-sign": cmd_roster_sign,
                "withdraw": cmd_withdraw, "say": cmd_say, "manage": cmd_manage, "countersign": cmd_countersign}
    try:
        cfg = SonnetConfig.load(Path(args.config))
        return handlers[args.cmd](cfg, args)
    except SystemExit as e:
        return int(e.code or 0)
    except (ConfigError, package.PackageError, register.Disarmed, identity.IdentityError, ValueError) as e:
        print(f"erreur: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
