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

from .brain import Brain, Context, RuleBrain, cheap_prefilter, normalize_for_dupes, prefix_key
from .client import ApiError, Duplicate, Message, NetworkError, RateLimited, TechnocoreClient
from .config import Config
from .identity import Identity
from .safety import check_reply
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
        self.rules = brain if isinstance(brain, RuleBrain) else RuleBrain()
        self.state = state
        if cfg.mailbox_enabled and not state.mailbox:
            import secrets

            state.mailbox = "mb-p-" + secrets.token_hex(12)
            state.save()
            log.info("boite aux lettres creee: /r/%s (ecriture signee uniquement, jamais listee)", state.mailbox)
        self.rooms: list[str] = list(cfg.rooms)
        if cfg.mailbox_enabled and state.mailbox and state.mailbox not in self.rooms:
            self.rooms.append(state.mailbox)
        self.memory: dict[str, RoomMemory] = {r: RoomMemory(cfg.history_window) for r in self.rooms}
        # candidats accumules entre deux consultations du cerveau, et date de la derniere
        self.pending: dict[str, list[Message]] = {r: [] for r in self.rooms}
        self.last_think: dict[str, float] = {r: 0.0 for r in self.rooms}
        self.stop_requested = False

    # --- identite publique -------------------------------------------------

    def desired_note(self) -> str:
        parts = [self.ident.did, "agent:technocore-agent", f"nick:{self.cfg.nick}"]
        if self.cfg.mailbox_enabled and self.state.mailbox:
            parts.append(f"mailbox:{self.state.mailbox}")
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

        ctx = Context(my_did=self.ident.did, nick=self.cfg.nick, recent_texts=mem.counter, history=list(mem.history),
                      blocked=set(self.state.blocked), signed_only=self.cfg.signed_only)
        is_mailbox = room == self.state.mailbox
        mention_only = room in self.cfg.mention_only_rooms
        pending = self.pending[room]
        rule_candidates: list[Message] = []
        urgent = is_mailbox
        for msg in page.messages:
            if msg.sender != self.ident.did and cheap_prefilter(msg, ctx):
                if mention_only and not ctx.mentions_me(msg.text):
                    rule_candidates.append(msg)  # room-torrent : les regles suffisent, pas le modele
                else:
                    pending.append(msg)
                    urgent = urgent or ctx.mentions_me(msg.text)
            mem.push(msg)  # apres le filtre : un texte ne doit pas se compter lui-meme comme repete
        del pending[:-self.cfg.max_candidates_per_poll]
        self.state.cursors[room] = page.last_seq
        self.state.save()

        if rule_candidates:
            quota = min(self.replies_left(room), 1)
            if quota > 0:
                self._post_decisions(room, self.rules.decide_batch(room, rule_candidates, ctx, quota),
                                     rule_candidates, page.last_seq)

        # Le cerveau n'est consulte qu'une fois par `think_seconds` et par room (ou tout de suite
        # si on est mentionne, ou dans la boite aux lettres) : lire souvent ne doit pas multiplier
        # les appels au modele.
        now = time.time()
        due = urgent or now - self.last_think[room] >= self.cfg.think_seconds
        if not pending or not due:
            return
        quota = min(self.replies_left(room), self.cfg.max_replies_per_round)
        if quota <= 0:
            log.info("%s: %d candidats en attente mais quota de reponses epuise", room, len(pending))
            return
        candidates, self.pending[room] = list(pending), []
        self.last_think[room] = now
        ctx.history = list(mem.history)
        self._post_decisions(room, self.brain.decide_batch(room, candidates, ctx, quota), candidates, page.last_seq)

    def _post_decisions(self, room: str, decisions: dict[int, str], candidates: list[Message], last_seq: int) -> None:
        by_seq = {m.seq: m for m in candidates}
        for seq in sorted(decisions):
            msg = by_seq[seq]
            blocked = self.may_reply(room, msg)
            if blocked:
                log.info("%s seq=%s: reponse retenue mais bloquee (%s)", room, seq, blocked)
                continue
            log.info("%s seq=%s <%s> %s", room, seq, msg.sender[-8:], msg.text[:160])
            reason = check_reply(decisions[seq])
            if reason:
                log.warning("%s seq=%s: reponse REFUSEE par le filtre de sortie (%s): %s",
                            room, seq, reason, decisions[seq][:160])
                if self.state.note_refusal(msg.sender, self.cfg.auto_block_after, reason):
                    log.warning("emetteur %s BLOQUE automatiquement (%s refus)", msg.sender, self.cfg.auto_block_after)
                self.state.save()
                continue
            self.post_and_confirm(room, decisions[seq], last_seq, reply_to=msg.sender)

    def replies_left(self, room: str) -> int:
        now = time.time()
        room_used = self.state.replies_in_room_since(room, 3600, now)
        total_used = sum(1 for r in self.state.recent_replies if now - r["at"] < 3600)
        return max(0, min(self.cfg.max_replies_per_room_per_hour - room_used, self.cfg.max_replies_per_hour - total_used))

    # --- boucle ------------------------------------------------------------

    def run(self, max_cycles: int | None = None) -> None:
        def _stop(signum, _frame):
            log.info("signal %s recu, arret propre apres ce tour", signum)
            self.stop_requested = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        log.info("demarrage: did=%s empreinte=%s rooms=%s poll=%ss cerveau=%s signes_seulement=%s bloques=%d",
                 self.ident.did, self.ident.fingerprint, ",".join(self.rooms), self.cfg.poll_seconds, self.brain.name,
                 self.cfg.signed_only, len(self.state.blocked))
        backoff = BACKOFF_MIN
        cycles = 0
        identity_published = False
        while not self.stop_requested:
            try:
                if not identity_published:
                    self.publish_identity()
                    identity_published = True
                for room in self.rooms:
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
