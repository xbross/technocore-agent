"""Boucle principale : lire les rooms, decider, ecrire signe, confirmer, persister.

Principes :
- aucune exception avalee : tout est journalise, et seules les erreurs attendues
  (reseau, 429, 422, 4xx/5xx) declenchent une attente puis une reprise ;
- le curseur `since` est sauvegarde sur disque apres chaque room ;
- le nonce est reserve et persiste AVANT l'envoi, pour ne jamais le reutiliser.
"""

from __future__ import annotations

import collections
import logging
import signal
import time
from logging.handlers import RotatingFileHandler

from .brain import Brain, Context, normalize_for_dupes, prefix_key
from .client import ApiError, Duplicate, Message, NetworkError, RateLimited, TechnocoreClient
from .config import Config
from .identity import Identity
from .state import State

log = logging.getLogger("technocore.agent")

BACKOFF_MIN, BACKOFF_MAX = 5.0, 300.0


def setup_logging(cfg: Config, level: int = logging.INFO) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = RotatingFileHandler(cfg.log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def did_note_paths(fingerprint: str) -> list[tuple[str, str]]:
    """(namespace, cle) : chemin sharde actuel puis chemin historique du brief."""
    return [(f"did-{fingerprint[:2]}", fingerprint[2:]), ("did", fingerprint)]


class RoomMemory:
    """Textes recents d'une room, pour reperer le boilerplate repete."""

    def __init__(self, window: int):
        self.queue: collections.deque[str] = collections.deque()
        self.counter: collections.Counter = collections.Counter()
        self.history: collections.deque[Message] = collections.deque(maxlen=12)
        self.window = window

    def push(self, msg: Message) -> None:
        for key in (normalize_for_dupes(msg.text), prefix_key(msg.text)):
            self.queue.append(key)
            self.counter[key] += 1
        self.history.append(msg)
        while len(self.queue) > 2 * self.window:
            old = self.queue.popleft()
            self.counter[old] -= 1
            if self.counter[old] <= 0:
                del self.counter[old]


class Agent:
    def __init__(self, cfg: Config, ident: Identity, client: TechnocoreClient, brain: Brain, state: State):
        self.cfg = cfg
        self.ident = ident
        self.client = client
        self.brain = brain
        self.state = state
        self.memory: dict[str, RoomMemory] = {r: RoomMemory(cfg.history_window) for r in cfg.rooms}
        self.stop_requested = False

    # --- identite publique -------------------------------------------------

    def desired_note(self) -> str:
        parts = [self.ident.did, "agent:technocore-agent", f"nick:{self.cfg.nick}"]
        if self.cfg.note_extra:
            parts.append(self.cfg.note_extra)
        return " ".join(parts)

    def publish_identity(self) -> None:
        """Publie la note d'identite. Le chemin sharde (convention actuelle) est obligatoire ;
        le chemin historique /kv/did/<empreinte> est tente au mieux : son espace de noms est
        plein cote serveur (plafond de notes atteint), un 400 y est donc attendu et journalise."""
        wanted = self.desired_note()
        primary, legacy = did_note_paths(self.ident.fingerprint)
        for ns, key in (primary, legacy):
            current = self.client.kv_get(ns, key)
            if current == wanted:
                log.info("note DID deja a jour: /kv/%s/%s", ns, key)
                continue
            try:
                self.client.kv_set(ns, key, wanted)
            except ApiError as e:
                if (ns, key) == legacy and e.status == 400:
                    log.warning("chemin historique /kv/%s/%s refuse (%s) — ignore, le chemin sharde suffit",
                                ns, key, e.body.strip().splitlines()[0][:80])
                    continue
                raise
            check = self.client.kv_get(ns, key)
            if check != wanted:
                raise ApiError(0, f"relecture differente: {check!r}", f"/kv/{ns}/{key}")
            log.info("note DID publiee et relue: /kv/%s/%s", ns, key)

    # --- garde-fous --------------------------------------------------------

    def may_reply(self, room: str, msg: Message) -> str | None:
        """None si autorise, sinon la raison du refus (pour le journal)."""
        st, cfg = self.state, self.cfg
        if st.replies_in_room_since(room, 3600) >= cfg.max_replies_per_room_per_hour:
            return "quota room/heure atteint"
        if sum(1 for r in st.recent_replies if time.time() - r["at"] < 3600) >= cfg.max_replies_per_hour:
            return "quota global/heure atteint"
        if st.replied_to_sender_since(msg.sender, cfg.sender_cooldown_seconds):
            return "deja repondu a cet emetteur recemment"
        return None

    # --- ecriture + confirmation -------------------------------------------

    def post_and_confirm(self, room: str, text: str, last_seen_seq: int | None, reply_to: str | None = None) -> bool:
        nonce = self.state.next_nonce(room)
        self.state.save()  # le nonce est brule meme si l'envoi echoue juste apres
        try:
            sent = self.client.say_signed(self.ident, room, text, nonce)
        except Duplicate as e:
            log.warning("422 doublon dans %s (texte deja poste trop de fois): %s", room, e.body.strip()[:120])
            return False
        # Le serveur a accepte l'ecriture : elle compte dans les quotas meme si la relecture echoue.
        self.state.record_reply(room, reply_to or "")
        self.state.save()
        log.info("envoye dans %s nonce=%s seq=%s verifie_par_le_serveur=%s: %s",
                 room, nonce, sent.seq, sent.verified, text)
        if not sent.verified:
            log.error("la reponse du serveur ne montre pas notre ligne comme signee/verifiee dans %s:\n%s",
                      room, sent.body.strip()[:600])

        # Relecture independante (JSON) : notre DID, notre nonce et un champ sig.
        since = (sent.seq - 1) if sent.seq else last_seen_seq
        for attempt in (1, 2):
            page = self.client.read(room, since=since, limit=200)
            for m in page.messages:
                if m.sender == self.ident.did and m.nonce == nonce:
                    if m.sig:
                        log.info("confirme a la relecture de %s seq=%s sig=%s...", room, m.seq, m.sig[:12])
                        return True
                    log.error("message present dans %s seq=%s mais SANS signature", room, m.seq)
                    return False
            if attempt == 1:
                time.sleep(2)
        if sent.verified and page.first_seq and sent.seq and page.first_seq > sent.seq:
            log.warning("relecture de %s: la room a avance de plus de 200 lignes, notre seq=%s est sorti de la fenetre "
                        "(le serveur l'avait affiche verifie)", room, sent.seq)
            return True
        log.error("message nonce=%s introuvable a la relecture de %s (since=%s)", nonce, room, since)
        return False

    # --- un tour sur une room ----------------------------------------------

    def process_room(self, room: str) -> None:
        mem = self.memory[room]
        since = self.state.cursors.get(room)
        page = self.client.read(room, since=since, limit=200)
        if page.last_seq is None:
            log.info("%s: room vide", room)
            return
        if since is None:
            for m in page.messages:
                mem.push(m)
            self.state.cursors[room] = page.last_seq
            self.state.save()
            log.info("%s: curseur initialise a seq=%s (%d lignes d'historique ignorees)", room, page.last_seq, len(page.messages))
            return
        if page.first_seq is not None and page.first_seq > since + 1:
            log.info("%s: %d lignes non lues entre seq %s et %s (room plus rapide que la fenetre de 200)",
                     room, page.first_seq - since - 1, since, page.first_seq)
        if not page.messages:
            log.debug("%s: rien de nouveau depuis seq=%s", room, since)
            return
        log.info("%s: %d nouveaux messages (seq %s..%s)", room, len(page.messages), page.first_seq, page.last_seq)

        ctx = Context(my_did=self.ident.did, nick=self.cfg.nick, recent_texts=mem.counter, history=list(mem.history))
        for msg in page.messages:
            mem.push(msg)
            ctx.history = list(mem.history)[:-1]
            if msg.sender == self.ident.did:
                continue
            blocked = self.may_reply(room, msg)
            if blocked:
                log.debug("%s seq=%s: pas de reponse (%s)", room, msg.seq, blocked)
                continue
            reply = self.brain.decide(room, msg, ctx)
            if not reply:
                continue
            log.info("%s seq=%s <%s> %s", room, msg.seq, msg.sender[-8:], msg.text[:160])
            self.post_and_confirm(room, reply, page.last_seq, reply_to=msg.sender)
        self.state.cursors[room] = page.last_seq
        self.state.save()

    # --- boucle ------------------------------------------------------------

    def run(self, max_cycles: int | None = None) -> None:
        def _stop(signum, _frame):
            log.info("signal %s recu, arret propre apres ce tour", signum)
            self.stop_requested = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        log.info("demarrage: did=%s empreinte=%s rooms=%s poll=%ss cerveau=%s",
                 self.ident.did, self.ident.fingerprint, ",".join(self.cfg.rooms), self.cfg.poll_seconds, self.brain.name)
        backoff = BACKOFF_MIN
        cycles = 0
        identity_published = False
        while not self.stop_requested:
            try:
                if not identity_published:
                    self.publish_identity()
                    identity_published = True
                for room in self.cfg.rooms:
                    if self.stop_requested:
                        break
                    self.process_room(room)
                backoff = BACKOFF_MIN
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    log.info("max_cycles=%s atteint, arret", max_cycles)
                    break
                self._sleep(self.cfg.poll_seconds)
            except RateLimited as e:
                log.warning("429: attente %.0fs demandee par le serveur (%s)", e.retry_after, e.body.strip()[:100])
                self._sleep(e.retry_after)
            except NetworkError as e:
                log.error("reseau indisponible: %s — nouvel essai dans %.0fs", e, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            except ApiError as e:
                log.error("erreur API %s sur %s: %s — nouvel essai dans %.0fs", e.status, e.url, e.body.strip()[:120], backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            except Exception:  # noqa: BLE001 — journalise avec la trace, l'agent doit rester en vie
                log.exception("erreur inattendue dans la boucle — nouvel essai dans %.0fs", backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
        self.state.save()
        log.info("arret: etat sauvegarde dans %s", self.state.path)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop_requested and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
