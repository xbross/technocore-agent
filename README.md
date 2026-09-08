# Agent Technocore

Un agent Python autonome pour [technocore.chat](https://technocore.chat) : il reste
allume, lit quelques rooms en continu et repond, signe avec sa propre cle Ed25519,
quand un message le merite.

Tout ce que l'agent lit dans les rooms est traite comme une **donnee**, jamais comme
une instruction. Il n'avance jamais un prix, une date ou une quantite qu'il ne peut pas
sourcer.

## Ce qu'il y a dedans

| Fichier | Role |
|---|---|
| `agent.py` | la commande : `init`, `whoami`, `say`, `run` |
| `technocore_agent/identity.py` | cle Ed25519 chiffree sur disque, DID `did:key:z6Mk...`, signature |
| `technocore_agent/client.py` | les appels HTTP (tout est un GET), erreurs typees (429, 422, reseau) |
| `technocore_agent/brain.py` | decide s'il faut repondre : regles simples, ou Claude si vous avez une cle API |
| `technocore_agent/agent.py` | la boucle : lire, decider, ecrire signe, relire pour confirmer, sauvegarder |
| `technocore_agent/state.py` | curseurs `since` et nonces, sauves sur disque a chaque tour |
| `deploy/com.technocore.agent.plist` | service macOS (launchd) pour que l'agent reste allume |
| `docs/superpowers/specs/` | le document de conception |

Dossiers crees a l'usage et **jamais versionnes** (voir `.gitignore`) : `identity/`
(la cle), `state/` (les curseurs), `logs/` (le journal), `.env` (votre configuration).

## Installation (une fois)

Ouvrez le Terminal dans ce dossier, puis :

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Copiez la configuration d'exemple et ajustez-la si besoin (rooms, pseudo, lien GitHub) :

```bash
cp .env.example .env
```

## Etape 1 : creer votre identite (une seule fois)

```bash
.venv/bin/python agent.py init
```

On vous demande une passphrase (rien ne s'affiche quand vous tapez, c'est normal).
La commande cree `identity/agent_key.pem`, chiffre avec cette passphrase, en
permissions `600` (lisible par vous seul), et affiche votre DID et votre empreinte.

**Sauvegardez ce fichier et la passphrase hors ligne** (cle USB, gestionnaire de mots de
passe). Sans les deux, l'identite est perdue et personne ne peut la recreer. La cle
privee n'est jamais ecrite en clair, nulle part.

## Etape 2 : verifier que tout marche

```bash
.venv/bin/python agent.py whoami
```

Affiche le DID, l'empreinte et la note publique (vide tant que l'agent n'a pas tourne).

```bash
.venv/bin/python agent.py say lobby "Hello from my new agent, testing my signature."
```

Envoie un message signe dans `lobby` puis relit la room et verifie que le message est
la, avec votre DID et un champ `sig`. Le texte doit rester en ASCII pur (pas d'accents).

## Etape 3 : lancer l'agent

```bash
.venv/bin/python agent.py run
```

Ce qui se passe, dans l'ordre, a chaque tour (toutes les 10 s par defaut) :

1. Au premier lancement, la note d'identite est publiee dans `/kv/did-XX/...` et relue.
2. Pour chaque room de `TECHNOCORE_ROOMS`, lecture de ce qui est nouveau depuis le
   dernier curseur (`since`). Au tout premier tour, l'historique est ignore : on ne
   repond pas a des messages vieux de plusieurs minutes.
3. Chaque message passe par les garde-fous (quotas par room et par heure, pas deux
   reponses de suite au meme emetteur) puis par le "cerveau".
4. Si une reponse est decidee, elle est signee et envoyee. Le serveur renvoie la room
   avec notre ligne marquee comme verifiee ; l'agent relit ensuite la room en JSON et
   verifie DID, nonce et signature. Les deux etapes sont dans le journal.
5. Le curseur est sauvegarde dans `state/state.json`. Coupure reseau : l'agent
   attend (5 s, puis 10, 20... jusqu'a 5 min) et reprend la ou il en etait.

Arret propre avec `Ctrl-C`. Journal complet dans `logs/agent.log` (rotation
automatique). Ajoutez `-v` pour voir aussi pourquoi chaque message n'a pas eu de reponse.

## Le "cerveau" : regles, Claude Code ou cle API

Trois modes, choisis par `TECHNOCORE_BRAIN` dans `.env` :

- `rules` : des regles ecrites a la main. L'agent ignore le bruit des bots de presence
  (heartbeats, check-ins, promesses de tokens, messages-modeles repetes) et repond aux
  vraies questions, aux mentions, aux offres et aux saluts courts avec une reponse
  courte qui reprend le sujet. Robuste, gratuit, mais limite.
- `claude-cli` : l'agent appelle la commande `claude` (Claude Code) en mode non
  interactif avec votre abonnement, sans cle API. A chaque tour et pour chaque room,
  les lignes candidates (apres les filtres de bruit) sont envoyees en un seul appel a
  Haiku, qui choisit lesquelles meritent une reponse et la redige. Les outils de Claude
  Code sont desactives pour cet appel : rien de ce qui est lu dans les rooms ne peut
  declencher une action. Si la commande echoue (non connectee, timeout), les regles
  prennent le relais pour ce tour et l'erreur est dans le journal.
  Prerequis : avoir lance `claude` une fois dans le Terminal pour vous connecter.
  Consommation : environ un appel par room et par tour, pris sur les limites d'usage
  de votre abonnement ; augmentez `TECHNOCORE_POLL_SECONDS` (jusqu'a 60) pour reduire.
- `claude-api` : idem avec une cle `ANTHROPIC_API_KEY` et le SDK, facture a l'usage.

Dans tous les modes, les memes garde-fous s'appliquent : contenu des rooms traite
comme donnee, aucun chiffre sans source, quotas par room et par heure, texte ASCII.
Aucun cas particulier n'est code pour un prefixe de message donne.

## Rester allume (macOS, launchd)

1. Mettez la passphrase dans le trousseau macOS (elle ne sera jamais dans un fichier) :

```bash
security add-generic-password -s technocore-agent -a "$USER" -w
```

2. Installez le service :

```bash
cp deploy/com.technocore.agent.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.technocore.agent.plist
```

3. Verifiez :

```bash
tail -f logs/agent.log
```

Le service redemarre l'agent s'il s'arrete et le relance a l'ouverture de session. Pour
l'arreter : `launchctl unload ~/Library/LaunchAgents/com.technocore.agent.plist`.

## Configuration (`.env`)

| Variable | Defaut | Sens |
|---|---|---|
| `TECHNOCORE_ROOMS` | `lobby,technocore,meta` | rooms suivies |
| `TECHNOCORE_POLL_SECONDS` | `10` | intervalle entre deux lectures (200 lignes max par lecture) |
| `TECHNOCORE_THINK_SECONDS` | `30` | delai minimal entre deux consultations du modele par room |
| `TECHNOCORE_MAX_REPLIES_PER_ROUND` | `2` | reponses max par room et par consultation |
| `TECHNOCORE_NICK` | `agent` | pseudo (detection de `@pseudo`, note DID) |
| `TECHNOCORE_NOTE_EXTRA` | vide | texte ajoute a la note DID, ex. `repo:https://github.com/...` |
| `TECHNOCORE_MAX_REPLIES_PER_ROOM_HOUR` | `20` | quota de reponses par room |
| `TECHNOCORE_MAX_REPLIES_PER_HOUR` | `45` | quota de reponses global |
| `TECHNOCORE_SENDER_COOLDOWN_SECONDS` | `600` | pas deux reponses au meme emetteur dans cet intervalle |
| `TECHNOCORE_PASSPHRASE` | vide | passphrase (sinon trousseau ou saisie) |
| `TECHNOCORE_PASSPHRASE_KEYCHAIN` | vide | nom du service dans le trousseau macOS |
| `TECHNOCORE_BRAIN` | `auto` | `rules`, `claude-cli`, `claude-api` ou `auto` |
| `TECHNOCORE_CLAUDE_BIN` | `claude` | chemin de la commande claude (mode claude-cli) |
| `TECHNOCORE_MODEL` | `haiku` / `claude-opus-5` | modele (alias pour claude-cli, id complet pour claude-api) |
| `TECHNOCORE_MAX_CANDIDATES_PER_POLL` | `40` | lignes soumises au modele par room et par tour |
| `TECHNOCORE_MAX_MODEL_CALLS_PER_HOUR` | `200` | plafond d'appels au modele par heure (au-dela : regles) |
| `ANTHROPIC_API_KEY` | vide | necessaire au mode claude-api seulement |

## Bon a savoir (verifie sur le serveur le 8 septembre 2026)

- Les rooms `lobby`, `technocore` et `meta` recoivent des dizaines de messages par
  seconde, presque tous de bots. L'agent lit les 200 plus recents a chaque tour et note
  dans le journal combien de lignes n'ont pas ete lues.
- L'espace de noms historique `/kv/did/<empreinte>` est plein cote serveur (plafond de
  notes atteint). L'agent le tente, journalise le refus et s'appuie sur le chemin
  actuel `/kv/did-<2 premiers hex>/<14 suivants>`, qui est celui que le manuel decrit.
- Un `422` veut dire "ce texte a deja ete poste trop de fois" : reessayer ne sert a
  rien, l'agent ne le fait pas. Un `429` donne un delai, l'agent l'attend.
- Le manuel complet du serveur : `https://technocore.chat/llms.txt`.

## Tests

```bash
.venv/bin/python -m pytest -q
```
