# BotRadar

Statut : présentation et prise en main. Les règles de contribution sont dans
[AGENTS.md](AGENTS.md) ; les procédures liées exigent leurs permissions propres.

[CI](https://github.com/ZeAmine/bot-radar/actions/workflows/ci.yml)

Bot Telegram privé d'aide à la décision pour l'occasion : BD/comics, vêtements,
tech et jeux vidéo. Il détecte les annonces Vinted, applique un matching local,
compare les BD au marché d'occasion livré et envoie l'alerte avant les enrichissements.
Utilisateur unique protégé par `TELEGRAM_ALLOWED_USER_IDS`.

L'IA propose variantes, exclusions, conseils de négociation et de revente.
Le bot ne paie jamais et ne choisit aucun relais. Le lien « Voir l'annonce » est
l'accès manuel à l'annonce. Aucun userscript, extension ou script injecté.

## Comprendre ou modifier le projet

| Besoin | Lecture |
| --- | --- |
| Couches et points d'entrée | [Architecture](docs/architecture.md) |
| Ingestion et ordre d'envoi | [Contrat d'alerte](docs/contracts/alert-pipeline.md) |
| ISBN, références de prix et lots | [Catalogue et marché](docs/contracts/catalogue-market.md) |
| Interface Telegram et moteur IA | [Telegram et IA](docs/contracts/telegram-ai.md) |
| Permissions, worktrees et reprise | [Workflow agents](docs/agent-workflow.md) |
| Pourquoi ces choix | [Décisions](docs/decisions/README.md) |
| Écarts connus | [Points ouverts](docs/open-issues.md) |

## Installation locale

Prérequis : Python 3.12, PostgreSQL 16 et Redis 7 ; Docker/Compose si utilisés.
Une installation, migration ou mise en route nécessite une autorisation adaptée.
Les tests synthétiques ne nécessitent pas de démarrer l'application.

### Installation pas-à-pas

1. **Cloner avec le sous-module épinglé** (réseau nécessaire) :
   ```bash
   git clone --recurse-submodules https://github.com/ZeAmine/bot-radar.git
   cd bot-radar
   # Dans un clone existant : git submodule update --init
   # Ne pas utiliser --remote : aucun upgrade automatique de Superpowers.
   ```

2. **Créer un venv Python 3.12 et installer avec le verrou** :
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -c requirements.lock -e ".[dev]"
   make check-lock
   ```

3. **Configurer avant de démarrer les services** : copier `.env.example` vers un
   nouveau `.env` local uniquement s’il n’existe pas, puis renseigner les paramètres.
   L’exemple vise l’application dans Docker (`postgres`, `redis`). Pour Uvicorn
   sur l’hôte, utiliser `localhost:5432` dans `DATABASE_URL` et
   `redis://localhost:6379/0` dans `REDIS_URL`. Les identifiants PostgreSQL doivent
   correspondre aux substitutions Compose. Aucun fichier réel n’est versionné.

4. **Services locaux** : ce Compose référence un réseau externe `web_gateway`.
   Il doit exister avant son utilisation ; sa création éventuelle est explicite
   (`docker network create web_gateway`) et n’est pas un déploiement de Caddy.
   ```bash
   docker compose up -d postgres redis
   ```
   Les noms et ports de ce Compose sont fixes : ne pas le dupliquer dans plusieurs
   worktrees. Pour les tests, préférer leurs bases isolées et leurs doubles.

5. **Application sur l’hôte**, après configuration et disponibilité des services :
   ```bash
   alembic upgrade head
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```
   Le scheduler démarre ; désactiver le poller n’empêche pas les jobs de marché.
   Cette commande peut appeler des services externes : elle ne sert pas de recette
   de vérification isolée. Pour les agents, appliquer le mandat décrit dans AGENTS.md.


## Configuration

[.env.example](.env.example) inventorie les paramètres applicatifs de
[Settings](app/core/config.py). Ne jamais committer un fichier réel ou un export de
session. Compose exige séparément `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
non vides et cohérents avec `DATABASE_URL` et la base existante.
`TELEGRAM_WEBHOOK_URL`, `EBAY_ENVIRONMENT` et `DOMAIN_NAME` ne sont pas lus par
l'application ; eBay utilise `EBAY_ENV`.

Le moteur IA est choisi par `AI_PROVIDER`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`,
`AI_TIMEOUT_SECONDS`, `AI_MAX_TOKENS` et `AI_REASONING_EFFORT`.
Par défaut : River, `deepseek-ai/DeepSeek-V4.1-Flash-1017`, raisonnement `none`,
sortie plafonnée à 2 048 tokens. OpenAI/Luna reste disponible mais inactif.
Changer de fournisseur enregistré ne nécessite pas de changer les handlers.
[Bascule, évaluation et rollback](docs/operations/llm-migration.md).
La réponse de `/health` ne garantit pas une génération IA ni la capacité métier.
Pour River, l'état IA est passif : non vérifié avant le premier appel réel, puis
dernier appel réussi ou échoué. La santé ne déclenche aucune requête River.
OpenAPI (`/docs`, `/redoc`, `/openapi.json`) est désactivé en production seulement.

## Vérification et commandes

[Makefile](Makefile) définit les commandes ; [la CI](.github/workflows/ci.yml)
contrôle Ruff, mypy et pytest, plus les tests d'intégration PostgreSQL.

| Commande | Effet / autorisation à vérifier |
| --- | --- |
| `make lint` | Ruff et mypy |
| `make test` | Pytest ; isoler configuration et transports avant imports |
| `make test-isolated` | Recette existante, configuration factice et transports refusés ; aucun service démarré |
| `make bench` | Benchmarks synthétiques, sans autre vérification concurrente |
| `make check-lock` | Cohérence locale du verrou |
| `make check-history` | Worktrees temporaires et environnement hérité : mandat explicite |
| `make dev` / `make up` | Démarre application/services ; pas une vérification isolée |
| `make migrate` | Modifie la base cible |
| `make pr` / `make verify` | Livraison Git et contrôles GitHub : réseau et mandat |
| `make deploy` | Production : lire obligatoirement la procédure ci-dessous |

Pour un agent sans services/secrets, utiliser les recettes décrites dans
[le workflow](docs/agent-workflow.md). Un résultat est attribué à la révision,
au diff et à la sélection exécutée, jamais à un nombre de tests permanent.

## Opérations et diagnostics

- [Déploiement et interruption](docs/operations/deployment.md) : procédure canonique,
  contrôles et limites ; un push ne déploie rien.
- [Préparation BD](docs/bd-preparation.md), [pause protégée](docs/pause-actions.md)
  et [retrait ISBN](docs/isbn-removal.md) : transactions, version et reprise.
- [Notification durable](docs/notification-delivery.md) et [latence](docs/alert-latency.md) :
  distinguer transport, persistance, réception et mesures locales.
- [Diagnostic matching](docs/matching-diagnostics.md) : expliquer un couple et
  évaluer le corpus versionné sans collecte ni `.env`.
- [Sauvegarde et récupération](docs/backup-recovery.md) : outillage synthétique,
  pas une exploitation réelle activée.

En cas de 401, vérifier la configuration du secret attendu sans afficher sa valeur.
En cas de cote absente, distinguer indisponibilité, aucune offre, lot inconnu et
catalogue épuisé selon le contrat marché ; ne pas relancer une collecte avant Telegram.
