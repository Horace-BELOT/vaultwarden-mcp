# vaultwarden-mcp

Serveur MCP qui donne à un agent (Claude Code…) un accès **en recherche** à un coffre
Vaultwarden, et la possibilité d'**écrire un secret dans un fichier** après validation
humaine, **sans que la valeur du secret passe jamais dans la conversation**.

- [Comment ça marche](#comment-ça-marche)
- [Ce qui est garanti, et ce qui ne l'est pas](#ce-qui-est-garanti-et-ce-qui-ne-lest-pas)
- [Installation](#installation) : [macOS](#macos) · [Linux](#linux) · [Windows](#windows)
- [Brancher sur Claude Code](#brancher-sur-claude-code)
- [Utilisation](#utilisation)
- [Options](#options)
- [Dépannage](#dépannage)
- [Développement](#développement)

## Comment ça marche

```
Claude Code ──search_secrets("stripe")──▶ vaultwarden-mcp ──bw list items──▶ coffre local (chiffré)
            ◀── noms, dossiers, URL, noms des champs (aucune valeur) ──

Claude Code ──get_secret_to_file(id, "password", "~/proj/.env", env_key="STRIPE_KEY")──▶
   ▲  Claude Code demande ta validation (à chaque appel, impossible à pré-autoriser)
   └─ vaultwarden-mcp écrit STRIPE_KEY='…' dans ~/proj/.env et répond « écrit, 32 caractères »
```

1. **Connexion (une fois par machine)** : tu fais `bw login` toi-même dans un terminal ; la 2FA
   passe par toi. Le serveur ne connaît ni ton e-mail ni ton mot de passe.
2. **Déverrouillage (une fois par session)** : au premier appel, le serveur ouvre une **fenêtre
   native** où tu tapes ton mot de passe maître. Il va directement au CLI Bitwarden, pas à
   l'agent. La clé de session reste dans la mémoire du serveur ; elle est oubliée après
   60 min d'inactivité et à l'arrêt du serveur.
3. **Recherche** (`search_secrets`) : par nom, identifiant, URL, dossier, collection, nom de
   champ personnalisé et contenu des notes. Ne renvoie que des métadonnées, plus la liste des
   champs récupérables (`available_fields`).
4. **Récupération** (`get_secret_to_file`) : l'outil porte l'annotation
   [`anthropic/requiresUserInteraction`](https://code.claude.com/docs/en/mcp.md) : Claude Code
   affiche une demande de validation **à chaque appel**, dans tous les modes de permission
   (y compris auto et bypass), sans option « ne plus demander ». La valeur est écrite dans un
   fichier (droits `600`) ou dans une clé d'un fichier `.env` ; l'agent ne reçoit que le
   chemin et la longueur.

Outils exposés : `vault_status`, `search_secrets`, `get_secret_to_file`, `sync_vault`,
`lock_vault`. Aucun outil n'écrit ni ne supprime dans le coffre.

## Ce qui est garanti, et ce qui ne l'est pas

Garanti par le serveur :

- aucune réponse d'outil ne contient de valeur secrète (mot de passe, valeur de champ, note,
  code TOTP) ; c'est vérifié par les tests ;
- chaque `get_secret_to_file` passe par ta validation dans Claude Code ;
- le mot de passe maître et la clé de session ne passent jamais par l'agent ;
- un fichier dans un dépôt git **non ignoré** par git est refusé (sauf
  `allow_not_gitignored=true`, visible dans la demande de validation) ;
- chaque déverrouillage et chaque secret écrit est journalisé (sur stderr, en JSON : élément,
  champ, destination, raison ; jamais la valeur).

Pas garanti, à connaître :

- **Le fichier écrit est lisible par l'agent.** Le serveur lui demande de ne pas le lire,
  mais rien ne l'en empêche techniquement. Préfère écrire dans le `.env` que lit ton
  application. Pour verrouiller, ajoute une règle `deny` dans Claude Code
  (`"Read(//chemin/du/dossier/**)"`) et active le sandbox.
- **Ne mets jamais `BW_SESSION` dans ton shell** (`export BW_SESSION=…` après `bw unlock`) :
  l'agent pourrait alors lancer `bw get password …` lui-même et contourner tout ça.
- **La recherche dans les notes est un oracle** : un agent pourrait deviner le contenu d'une
  note par des recherches successives. Lance le serveur avec `--no-search-notes` si tes notes
  contiennent des secrets (un fichier `.env` collé dans une note, par exemple).
- `lock_vault` et l'arrêt du serveur lancent `bw lock`, qui verrouille aussi une session
  `bw` ouverte dans ton terminal.

## Installation

Il faut, sur chaque machine :

1. **uv** (gère Python et les dépendances) ;
2. **le CLI Bitwarden `bw`**, connecté au serveur Vaultwarden ;
3. **ce dépôt**, cloné ;
4. **l'enregistrement dans Claude Code** (section suivante).

Dans les commandes ci-dessous, remplace `https://vault.example.com` par l'URL de ton
serveur Vaultwarden.

### macOS

Testé de bout en bout (macOS 26, `bw` 2026.10.1 via Homebrew, Vaultwarden
auto-hébergé).

```sh
# 1. uv
brew install uv                    # ou : curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. CLI Bitwarden (la formule s'appelle bitwarden-cli, la commande bw)
brew install bitwarden-cli
bw config server https://vault.example.com
bw login                           # e-mail, mot de passe maître, puis code 2FA
bw status                          # doit afficher "status":"locked"

# 3. le dépôt
git clone https://github.com/Horace-BELOT/vaultwarden-mcp.git ~/tools/vaultwarden-mcp
cd ~/tools/vaultwarden-mcp && uv sync
```

La fenêtre de déverrouillage est une boîte de dialogue `osascript`. Au premier lancement,
macOS peut demander d'autoriser l'app qui lance le serveur (Terminal, iTerm…) à contrôler
« System Events » : accepte.

### Linux

```sh
# 1. uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. CLI Bitwarden : une des trois méthodes officielles
sudo snap install bw                                    # Snap
# ou : npm install -g @bitwarden/cli                    # npm (Node.js requis ; arm64 : npm)
# ou : exécutable natif, https://bitwarden.com/download/?app=cli&platform=linux
#      unzip bw-linux-*.zip && chmod +x bw && sudo mv bw /usr/local/bin/

bw config server https://vault.example.com
bw login
bw status

# 3. la fenêtre de déverrouillage : zenity (GNOME, la plupart des bureaux) ou kdialog (KDE)
sudo apt install zenity            # Debian/Ubuntu ; dnf install zenity ; pacman -S zenity

# 4. le dépôt
git clone https://github.com/Horace-BELOT/vaultwarden-mcp.git ~/tools/vaultwarden-mcp
cd ~/tools/vaultwarden-mcp && uv sync
```

Sans session graphique (SSH, serveur), il n'y a pas de fenêtre : utilise
[`--password-command`](#mot-de-passe-maître-sans-fenêtre).

### Windows

```powershell
# 1. uv
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. CLI Bitwarden : une des méthodes officielles
choco install bitwarden-cli                             # Chocolatey
# ou : npm install -g @bitwarden/cli                    # npm (Node.js requis)
# ou : exécutable natif, https://bitwarden.com/download/?app=cli&platform=windows
#      dézipper bw.exe dans un dossier du PATH

bw config server https://vault.example.com
bw login
bw status

# 3. le dépôt
git clone https://github.com/Horace-BELOT/vaultwarden-mcp.git $HOME\tools\vaultwarden-mcp
cd $HOME\tools\vaultwarden-mcp; uv sync
```

La fenêtre de déverrouillage est une fenêtre Windows Forms lancée par Windows PowerShell 5.1
(présent sur tout Windows 10/11).

### Statut des tests par système

| | Tests automatiques (CI, avec le vrai `bw`) | Essai réel de bout en bout |
| --- | --- | --- |
| macOS | ✅ | ✅ fenêtre, déverrouillage, recherche dans un vrai coffre |
| Linux | ✅ | ❌ pas encore : fenêtre zenity/kdialog à confirmer |
| Windows | ✅ | ❌ pas encore : fenêtre PowerShell à confirmer |

Si tu fais l'essai sur Linux ou Windows, mets ce tableau à jour.

## Brancher sur Claude Code

```sh
# macOS / Linux
claude mcp add --scope user vault -- uv run --directory ~/tools/vaultwarden-mcp vaultwarden-mcp

# Windows (PowerShell)
claude mcp add --scope user vault -- uv run --directory $HOME\tools\vaultwarden-mcp vaultwarden-mcp
```

- `--scope user` : disponible dans tous tes projets.
- Le nom `vault` donne des outils `mcp__vault__search_secrets`, etc.
- Les options du serveur se mettent à la fin, par exemple
  `… vaultwarden-mcp --no-search-notes --idle-lock-minutes 30`.

Vérifie avec `claude mcp list` (`vault: … ✔ Connected`), puis **ouvre une nouvelle session**
Claude Code : les serveurs MCP sont chargés au démarrage.

Pour ne pas valider chaque recherche, autorise les outils en lecture dans
`~/.claude/settings.json`. `get_secret_to_file` restera soumis à validation quoi qu'il arrive :

```json
{
  "permissions": {
    "allow": ["mcp__vault__search_secrets", "mcp__vault__vault_status"]
  }
}
```

## Utilisation

Demande simplement à l'agent, par exemple :

- « Qu'est-ce qu'on a dans le coffre pour Stripe ? »
- « Mets le token GitHub de deploy-bot dans `.env` sous `GITHUB_TOKEN`. »

Déroulé :

1. Premier appel de la session : une fenêtre « Vaultwarden » s'ouvre, tape ton mot de passe
   maître.
2. Pour `get_secret_to_file`, Claude Code affiche la demande de validation avec l'élément, le
   champ, la destination et la raison donnée par l'agent. Vérifie-les avant d'accepter.
3. L'agent reçoit `{"destination": "...", "mode": "env_added", "length": 40}`, jamais la
   valeur.

Champs récupérables (`field`) : `username`, `password`, `totp` (le code courant), `notes`,
`field:<nom du champ personnalisé>`, `card.<clé>`, `sshKey.<clé>`. Ceux qui existent pour un
élément sont listés dans `available_fields`.

## Options

| Option | Défaut | Effet |
| --- | --- | --- |
| `--bw <chemin>` | `bw` du PATH | chemin du CLI Bitwarden |
| `--idle-lock-minutes <n>` | 60 | reverrouille après n minutes sans appel |
| `--cache-minutes <n>` | 5 | durée de réutilisation de la liste des éléments |
| `--no-search-notes` | recherche dans les notes | n'y cherche plus (voir l'oracle plus haut) |
| `--password-command "<cmd>"` | fenêtre native | commande qui imprime le mot de passe maître |

### Mot de passe maître sans fenêtre

`--password-command` lance une commande (sans shell) dont la sortie est le mot de passe
maître : utile sans session graphique, ou pour le lire dans le trousseau de l'OS. Attention :
le déverrouillage se fait alors sans que tu tapes quoi que ce soit ; la validation de chaque
`get_secret_to_file` reste, elle, obligatoire.

```sh
# macOS : trousseau
security add-generic-password -s vaultwarden-mcp -a "$USER" -w     # saisie du mot de passe
claude mcp add --scope user vault -- uv run --directory ~/tools/vaultwarden-mcp vaultwarden-mcp \
  --password-command "security find-generic-password -s vaultwarden-mcp -w"

# Linux : libsecret (paquet libsecret-tools)
secret-tool store --label=vaultwarden-mcp service vaultwarden-mcp
# puis --password-command "secret-tool lookup service vaultwarden-mcp"
```

## Dépannage

| Symptôme | Cause et solution |
| --- | --- |
| `brew install bw` : « No available formula » | La formule s'appelle `bitwarden-cli`. |
| `claude mcp list` : vault failed, ou log `startup.failed` | `bw` introuvable dans le PATH de Claude Code : passe `--bw /chemin/complet/bw`. |
| « The Bitwarden CLI is not logged in » | Fais `bw config server …` puis `bw login` dans un terminal. |
| « Unlock failed (wrong master password?) » | Mauvais mot de passe maître ; le prochain appel redemande. |
| Pas de fenêtre sous Linux | Installe `zenity` ou `kdialog` ; sans écran, utilise `--password-command`. |
| `bw` : erreur de certificat | Certificat auto-signé : exporte `NODE_EXTRA_CA_CERTS=/chemin/ca.pem` (à passer avec `claude mcp add -e`). |
| « … is inside a git repository and not ignored » | Ajoute le fichier au `.gitignore` du projet, puis redemande. |
| Éléments récents absents | Demande un `sync_vault` (la liste est gardée 5 min). |

Les logs du serveur (lignes JSON `vault.unlocked`, `secret.written`…) sont écrits sur stderr ;
Claude Code les garde dans ses logs MCP (`claude --debug`).

## Développement

```sh
uv sync                            # environnement + outils de dev
uv run pre-commit install          # hooks pre-commit, commit-msg et pre-push
```

| Commande | Rôle |
| --- | --- |
| `uv run ruff check .` / `uv run ruff format .` | lint et format |
| `uv run mypy` | typage strict (`src` et `tests`) |
| `uv run vulture` | code mort |
| `uv run pytest` | tests ; ceux sur le vrai `bw` sont sautés s'il n'est pas installé |

- **Hooks** : ruff sur les fichiers indexés au commit ; toute la chaîne (ruff, mypy, vulture,
  pytest) au push, comme la CI ; messages de commit au format
  [Conventional Commits](https://www.conventionalcommits.org/) (`feat: …`, `fix: …`).
- **CI** (`.github/workflows/checks.yml`) : lint et typage sous Linux, tests sous Linux, macOS
  et Windows avec le vrai `bw` installé par npm.
- **Dépendances** : `uv add <paquet>` (ou `uv add --dev`) ; `uv.lock` est commité.
- **Structure** : `bitwarden.py` (modèles du JSON de `bw` + adaptateur subprocess), `vault.py`
  (session, cache, verrouillage), `catalog.py` (recherche et résumés, fonctions pures),
  `writer.py` (fichiers et `.env`), `prompts.py` (fenêtres par OS), `server.py` (outils MCP),
  `__main__.py` (assemblage). Les tests passent des faux en mémoire (`tests/fakes.py`), pas des
  mocks.
