# BotRadar 🛰️

[![CI](https://github.com/ZeAmine/bot-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/ZeAmine/bot-radar/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/Tests-versionnes-blue?style=flat&logo=pytest)](https://docs.pytest.org/)
[![Linter](https://img.shields.io/badge/Linter-Ruff%20strict-black?style=flat&logo=ruff)](https://astral.sh/ruff)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat&logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![aiogram](https://img.shields.io/badge/aiogram-3.13+-2CA5E0?style=flat&logo=telegram)](https://docs.aiogram.dev/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0%20Async-D71F00?style=flat&logo=sqlalchemy)](https://www.sqlalchemy.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=flat&logo=postgresql)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=flat&logo=redis)](https://redis.io/)
[![IA](https://img.shields.io/badge/IA-moteur--interchangeable-blueviolet?style=flat)](app/providers/llm/)
[![Architecture](https://img.shields.io/badge/Architecture-Single--Process%20Monolith-blue?style=flat)](AGENTS.md)

**BotRadar** est un bot Telegram **privé** qui détecte des annonces et aide à décider d’un achat de seconde main.

Il surveille quatre univers à forte valeur ajoutée :
- 📚 **BD & Comics / Mangas** (`bd_comics`) — Cotes du marché d'occasion livrée, gestion des tomes et éditions originales.
- 💻 **Tech & Informatique** (`tech`) — Détection de matériel avec exclusion automatique de composants HS ou pièces détachées.
- 🎮 **Jeux vidéo & Consoles** (`gaming`) — Filtrage strict des éditions PAL-FR (rejet des jaquettes étrangères ES/DE/IT sans VF).
- 👕 **Vêtements & Mode** (`clothes`) — Critères déclarés de marques, tailles, états et mots-clés ; aucune authentification de contrefaçon n’est garantie.

---

## Sommaire

1. [Philosophie & Positionnement Produit](#1-philosophie--positionnement-produit)
2. [Architecture Technique & Invariants](#2-architecture-technique--invariants)
3. [Clavier & Expérience Telegram](#3-clavier--expérience-telegram)
4. [Moteur de Matching Déterministe CPU](#4-moteur-de-matching-déterministe-cpu)
5. [Sous-système IA à moteur interchangeable](#5-sous-système-ia--moteur-interchangeable)
6. [Cote BD & Comparateur de Marché](#6-cote-bd--comparateur-de-marché)
7. [Poller Vinted & Détection Haute Fréquence](#7-poller-vinted--détection-haute-fréquence)
8. [Intégration Continue (CI) & Qualité de Code](#8-intégration-continue-ci--qualité-de-code)
9. [Prérequis & Installation Locale](#9-prérequis--installation-locale)
10. [Configuration des Variables d'Environnement (.env)](#10-configuration-des-variables-denvironnement-env)
11. [Commandes Utiles (Makefile)](#11-commandes-utiles-makefile)
12. [Gestion des Migrations Alembic](#12-gestion-des-migrations-alembic)
13. [Déploiement en Production (VPS)](#13-déploiement-en-production-vps)
14. [Exemples d'Utilisation API & Webhook](#14-exemples-dutilisation-api--webhook)
15. [Diagnostic des Erreurs Courantes](#15-diagnostic-des-erreurs-courantes)
16. [Sécurité & Confidentialité des Données](#16-sécurité--confidentialité-des-données)

---

## 1. Philosophie & Positionnement Produit

Sur les plateformes de seconde main (notamment Vinted), les affaires exceptionnelles ("pépites") disparaissent généralement en **60 à 90 secondes**. BotRadar est conçu pour maximiser la réactivité de l'acheteur ("snipe") tout en fournissant une analyse décisionnelle instantanée.

### Ce que BotRadar EST
- ⚡ **Détection prioritaire** : l'alerte précède les enrichissements externes. Le seuil local de 500 ms des benchmarks simulés ne mesure pas le délai depuis la publication ni une livraison Telegram réelle.
- 📊 **Comparateur de marché réel** : Lecture synchrone de la cote d’occasion livrée en cache local (eBay, Momox, Gibert, Recyclivre, Chasse-aux-livres).
- 🧠 **Copilote décisionnel et d'auto-apprentissage** : Estimation de revente, coach de négociation et affinage automatique des alertes via IA.
- 🔒 **Système strictement privé** : Mono-utilisateur avec filtrage par allowlist (`TELEGRAM_ALLOWED_USER_IDS`).

### Ce que BotRadar n'est pas (périmètre actuel)
- ❌ **Pas de bot d'achat automatique** : Aucun checkout automatique, aucun panier automatisé.
- ❌ **Pas de bot d'interaction marketplace intrusive** : Aucun message automatique envoyé aux vendeurs via l'API marketplace.
- ❌ **Pas de bot public multi-tenant** : Réservé à un usage privé et sécurisé.

---

## 2. Architecture Technique & Invariants

BotRadar est articulé autour d'un **monolithe asynchrone à processus unique** (`radar_app`) hébergeant l'API FastAPI, le bot Telegram aiogram 3, le scheduler unifié APScheduler et le poller Vinted intégré (ADR-001).

```
                      Passerelle HTTPS Caddy (caddy_gateway)
                                        │ (Réseau web_gateway)
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Conteneur radar_app (FastAPI + aiogram 3 + APScheduler + Poller Vinted)     │
│                                                                             │
│  [ Webhook Telegram ]    [ Ingestion HTTP ]        [ Vinted Poller ]        │
│          │                       │                         │ (API Interne)  │
│          ▼                       ▼                         ▼                │
│    FSM Handlers            Matching CPU              Dédoublonnage          │
│   (/add, /show...)        (listing_match)             (seen_ids)            │
│          │                       │                         │                │
│          │                       ├────────► [ ⚡ ALERTE PRIORITAIRE ]      │
│          │                       │                │                         │
│          │                       ▼                ▼ (safe_create_task)      │
│          │                  Cote BD Locale      Moteur IA / BNF              │
│          │                  (0 HTTP avant)    (Post-alerte asynchrone)      │
│          │                       ▼                ▼                         │
│  ┌───────────────────────────────┴───────────────────────────────┐          │
│  │                     SQLAlchemy 2.0 Async                      │          │
└──┴───────────────────────────────┬───────────────────────────────┴──────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
       PostgreSQL 16 (radar_postgres)   Redis 7 (radar_redis)
       - Annonces dédoublonnées         - FSM Storage aiogram
       - Alertes & règles utilisateurs  - Cache de sessions Vinted
       - Catalogue BD & cotes marché    - Rate limiter & verrous
```

### Invariants opérationnels stricts (Constitution AGENTS.md)

1. **Invariant n°1 — L'alerte Telegram d'abord** :
   - Dédoublonnage instantané `(source_name, external_id)`.
   - Matching en mémoire CPU locale (`evaluate_listing_against_alert`).
   - Cote BD : **cache local PostgreSQL uniquement** (`MarketPriceService.get_comparison_summary` est synchrone, zéro requête HTTP).
   - Envoi immédiat du message Telegram (`send_listing_alert`).
   - **En tâche de fond différée uniquement (`safe_create_task`)** : enrichissement IA avec le vrai `message_id` et association ISBN BNF dans une session BDD distincte (ADR-009).
   - *Interdiction absolue de bloquer l'alerte par un appel réseau externe (BNF, eBay, moteur IA).*

2. **Invariant n°2 — Un seul processus et un seul scheduler (ADR-001)** :
   - Aucun conteneur worker séparé (`radar_app` pilote l'API, les webhooks, APScheduler et le poller Vinted).
   - APScheduler démarre dans le cycle de vie (`lifespan`) de FastAPI avec `max_instances=1` et `coalesce=True`.

3. **Invariant n°3 — Secrets stricts et zéro fuite dans les logs** :
   - Variables sensibles encapsulées dans des `SecretStr` Pydantic.
   - Formatteur `JSONFormatter` masquant automatiquement Bearer, `token=`, `password=`, `key=` et `sk-`.
   - Refus strict de démarrage en environnement de production si l'un des 4 secrets cardinaux est manquant (`validate_production_secrets()`).

---

## 3. Clavier & Expérience Telegram

Chaque notification d'opportunité reçue sur Telegram intègre un clavier inline compact, ergonomique et sans distractions :

```text
[ Voir l'annonce ]
[ Prix du marché ]                    # BD uniquement
[ Faux positif ] [ Négocier ]
```

Le faux positif demande confirmation, puis affiche « Signalé » ; annuler restitue
les actions initiales. Ces boutons restent sans émoji dans tous ces états.

- **`[ Voir l'annonce ]`** : Ouvre directement la page de l'article dans l'application Vinted avec les moyens de paiement et sessions de l'utilisateur.
- **`[ Prix du marché ]`** *(BD uniquement)* : Consulte le détail des offres d'occasion livrée (eBay, Momox, Gibert, Recyclivre, Chasse-aux-livres) et l'écart de prix.
- **`[ Faux positif ]`** : Enregistre le rejet de l'annonce pour alimenter le moteur d'apprentissage `/autotune`. Se transforme en `[ Signalé ]`.
- **`[ Négocier ]`** : Déclenche le coach IA pour obtenir une estimation d'offre pertinente et un message de négociation pré-rédigé courtois prêt à copier.

> **Note d'arbitrage (ADR-007 & ADR-011)** : Les anciens boutons factices `[ 💳 Payer ]` et `[ 🛍️ Dressing / Lot ]` ont été définitivement supprimés pour éliminer les erreurs de session et alléger l'interface.

### Commandes Telegram disponibles

| Commande | Rôle |
|---|---|
| `/add [titre]` | Assistant conversationnel (FSM) ou création guidée en langage naturel (alias : `/ajouter`) |
| `/addisbn [isbn]` | Création rapide d'une alerte BD directement ciblée par son code ISBN-13 |
| `/addurl [url]` | Création instantanée depuis une URL de recherche Vinted (analyse syntaxique pure côté serveur) |
| `/show [recherche]` | Affichage interactif et paginé des alertes configurées |
| `/list [catégorie]` | Synthèse compacte des alertes par catégorie ou globale |
| `/edit` | Modification granulaire d'une alerte (titre, mots-clés, exclusions, prix, états) |
| `/pause` / `/resume` | Suspension et réactivation instantanée d'une alerte |
| `/delete` | Suppression d'une alerte avec demande de confirmation |
| `/categories` | Liste des univers surveillés (`bd_comics`, `tech`, `gaming`, `clothes`) |
| `/stats` | Statistiques complètes d'activité sur 7 ou 30 jours |
| `/autotune` | Hub d'auto-tuning : analyse des faux positifs et suggestions d'exclusions IA (alias : `/tune`) |
| `/status` | Diagnostic de santé du service, du poller Vinted et du moteur IA (sonde HTTP) |
| `/cancel` | Annulation immédiate de l'opération ou du dialogue FSM en cours (alias : `/annuler`) |
| `/help` | Affichage du guide d'utilisation interactif |

---

## 4. Moteur de Matching Déterministe CPU

Le moteur de matching ([`app/domain/rules/listing_match.py`](app/domain/rules/listing_match.py)) évalue chaque annonce en mémoire CPU, sans appel fournisseur. La lecture groupée de l'état de notification (point 6) est la seule requête SQL du chemin, distincte du calcul de pertinence. Les budgets locaux sont ceux des scénarios simulés décrits dans [`docs/alert-latency.md`](docs/alert-latency.md) :

1. **Normalisation Unicode NFKD** : Suppression des accents, uniformisation des minuscules, des apostrophes typographiques et des tirets.
2. **Dictionnaire d'exclusions multilingue** (`MULTILINGUAL_EXCLUSION_MAPPING`) : Traduction et expansion automatique des exclusions en espagnol, allemand, italien et anglais (`boite vide` ➔ `caja vacia`, `nur ovp`, `solo scatola`, etc.).
3. **Filtre qualité Gaming anti-imports** : Rejet automatique des jeux vidéo en langues étrangères (DE, ES, IT) sauf mention explicite d'une édition PAL-FR ou VF.
4. **Exclusions matérielles par défaut** : Protection native contre les goodies, boîtes vides, accessoires orphelins (câbles seuls, chargeurs seuls) et articles défectueux ("pour pièces", "HS").
5. **Protection hybride BD / ISBN** :
   - Si un ISBN-13 est présent sur l'annonce, vérification exacte.
   - En l'absence d'ISBN textuel, fallback déterministe sur le titre de la série, le numéro de tome et l'auteur pour réduire les confusions sur les titres courts, sans garantie d’absence de faux positif (*"Blast"*, *"RIP"*, *"1629"*).
6. **Requête groupée** : Une seule requête SQL pour récupérer l'état de notification des alertes candidates (`match_notification_state`), aucun parcours itératif en base de données.

---

## 5. Sous-système IA — moteur interchangeable

L’enrichissement IA des annonces intervient après leur envoi. La création assistée et le coach de négociation utilisent aussi le moteur sur leurs propres parcours, sans ajouter d’appel avant l’envoi d’une annonce.

### Changer de modèle ne demande que quatre variables d'environnement

Le fournisseur n'est connu que dans [`app/providers/llm/`](app/providers/llm/). Tout le reste du projet parle au contrat `LLMClient` et ignore le moteur :

```bash
# .env — c'est tout.
AI_PROVIDER=openai-compatible   # ou anthropic
AI_BASE_URL=https://api.deepseek.com/v1
AI_MODEL=deepseek-flash
AI_API_KEY=sk-…
```

- **Groq, Mistral, OpenRouter, Together, Ollama, LM Studio** : compatible OpenAI, donc `AI_PROVIDER` reste `openai-compatible` et seules les trois dernières lignes changent.
- **Anthropic** (ou tout fournisseur au dialecte différent) : `AI_PROVIDER=anthropic` avec son adaptateur dédié dans [`anthropic.py`](app/providers/llm/anthropic.py).
- Un nouveau fournisseur non compatible OpenAI demande **un seul fichier** : une classe qui traduit vers son filaire, enregistrée dans [`registry.py`](app/providers/llm/registry.py). Ni handler, ni use case, ni test n'a besoin de changer.

[`tests/unit/test_llm_contract.py`](tests/unit/test_llm_contract.py) rejoue le même contrat sur **chaque** adaptateur enregistré — repli, forme de santé, cache court, transmission des images — donc la bascule est vérifiée, pas supposée.

### Cinq modules de conseil

Cinq modules vivants et spécialisés sont actifs ([`app/services/ai/modules/`](app/services/ai/modules/)) :

1. 🪄 **`natural_add`** (`AI_FEATURE_NATURAL_ADD=true`) : Parse une phrase libre en langage naturel pour pré-remplir les filtres d'une alerte lors de `/add`. L'ISBN qu'il propose est un simple candidat : il n'est retenu que si une notice bibliographique le confirme pour l'œuvre demandée (BNF en source primaire, OpenLibrary en repli), sinon l'alerte part sur le titre (ADR-014).
2. 💎 **`profit_scoring`** (`AI_FEATURE_PROFIT_SCORING=true`) : Calcule un score pépite, estime le prix de revente potentiel et la marge nette pour la tech, le gaming et les vêtements.
3. 💬 **`negotiation`** (`AI_FEATURE_NEGOTIATION=true`) : Analyse l'écart de prix et rédige un message de négociation poli et personnalisé à destination du vendeur.
4. ✨ **`keyword_variants`** (`AI_FEATURE_KEYWORD_VARIANTS=true`) : Suggère des variantes orthographiques, abréviations et fautes courantes, ainsi que des mots-clés d'exclusion **spécifiques à l'ouvrage** (auteur, éditeur, année et format issus de la fiche vérifiée), et non une liste de faux positifs générique (`[ ✨ Variantes & Exclusions IA ]`, ADR-014).
5. 🎯 **`auto_tuning`** (`AI_FEATURE_AUTO_TUNING=true`) : Analyse les faux positifs signalés par l'utilisateur (`[ Faux positif ]`) et génère des listes d'exclusions prêtes à copier ou modifier dans `/autotune` (ADR-008).

> **Épuration des modules obsolètes (ADR-006 & ADR-011)** : Les fonctionnalités expérimentales instables ou non pertinentes (`hidden_condition`, `vision_guard`, `bundle_decomposition`, `price_glitch`) ont été définitivement purgées du codebase.

---

## 6. Cote BD & Comparateur de Marché

Pour les bandes dessinées, comics et mangas, BotRadar compare en temps réel le coût de l'annonce avec le marché de l'occasion livrée :

- **Consultation synchrone à 0 HTTP** : L'alerte interroge exclusivement la table locale `book_market_cache`.
- **Calculateur de coût transparent** : Intègre le prix de l'article, la protection acheteur (0.70 € + 5%) et les frais de port forfaitaires (2.88 €).
- **Fournisseurs connectés au rafraîchissement planifié** :
  - **eBay Browse API** (OAuth2 client credentials).
  - **Momox** (prise en compte du port offert dès 19 € d'achat).
  - **Recyclivre** (frais de port inclus).
  - **Gibert** (articles d'occasion, port offert dès 30 €).
  - **Chasse-aux-livres** (historique et comparateur multi-vendeurs).
- **Tâches d'actualisation (`app/tasks/refresh_book_prices.py`)** :
  - Rafraîchissement global bi-quotidien à **07:00** et **19:00** Europe/Paris.
  - Rafraîchissement glissant incrémental à **:15** et **:45** (lots de 8 livres pour lisser la charge).
  - Nettoyage et purge des données obsolètes chaque nuit à **03:30** (`cleanup_old_data_task`).

---

## 7. Poller Vinted & Détection Haute Fréquence

Le poller Vinted intégré ([`app/tasks/vinted_poller.py`](app/tasks/vinted_poller.py)) applique une architecture optimisée (ADR-012) :

- 🚀 **API Interne JSON First (`/svc-catalogue/items`)** : Le catalogue JSON évite le coût du catalogue HTML complet ; volumes et latences dépendent de la réponse et ne constituent pas une garantie.
- 🛡️ **Repli Transparent (Fallback HTML)** : En cas d'anomalie sur le format JSON, le poller bascule automatiquement sur le catalogue HTML classique lorsque ce repli répond ; il ne garantit pas la disponibilité de la marketplace.
- ⚡ **Cadence Adaptative** : `VINTED_POLL_INTERVAL_SECONDS` (défaut **12 s**) fixe la cadence standard et plafonne les modes accélérés — proxy résidentiel à **8 s** en pointe (11h30-14h00 et 18h00-22h30 Paris) et **10 s** hors pointe (`VINTED_PROXY_URL`).
- 🌙 **Mode Nuit Automatique** : Suspension ou ralentissement de la scrutation entre 00:00 et 07:00 (Paris) pour préserver la réputation de l'IP.
- 🔀 **Ordonnancement Entrelacé (Staggered Interleaving)** : Espacement calculé (1,2s à 2,0s) entre les requêtes de différentes catégories pour lisser la charge réseau.
- 📷 **Photo de couverture** : L'annonce est affichée avec l'image de couverture renvoyée par `svc-catalogue/items` (voir ADR-012). Aucune requête Vinted supplémentaire n'est faite pour une annonce.

---

## 8. Intégration Continue (CI) & Qualité de Code

Le projet est outillé avec un pipeline d'Intégration Continue via GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) qui s'exécute à chaque push et pull request :

- **Ruff** : Linting strict conforme aux règles modernes Python 3.12 (longueur de ligne max 100 caractères, imports ordonnés, conventions PEP).
- **Pytest & Pytest-Asyncio** : tests versionnés d'ingestion, de matching, du FSM Telegram, du cache, du poller et de l'IA. Les six benchmarks locaux (`make bench`) utilisent des doubles et ne valident pas une latence depuis la publication ou une livraison réelle ; leurs frontières sont détaillées dans [`docs/alert-latency.md`](docs/alert-latency.md). Le nombre de tests et leur résultat dépendent de la révision et du périmètre exécutés.
- **Typage Statique (Mypy)** : `strict = true` dans `pyproject.toml` ; `mypy app scripts/matching_diagnostics.py` bloque la pull request en cas d'erreur. Le résultat vaut pour la révision exécutée.
- **Dépendances verrouillées** : [`requirements.lock`](requirements.lock) contraint les versions résolues par le Dockerfile et par la CI. Le verrou fixe les versions Python résolues ; il ne garantit pas une image identique entre plateformes ou versions des outils de construction. `make check-lock` échoue si une dépendance a été ajoutée à `pyproject.toml` sans être épinglée.

Pour exécuter les vérifications localement :

```bash
# Vérification du style et du linting
.venv/bin/ruff check .

# Exécution de la suite complète de tests
.venv/bin/pytest -v

# Benchmarks locaux avec transports simulés
make bench

# Typage statique
.venv/bin/mypy app scripts/matching_diagnostics.py
```

---

## 9. Prérequis & Installation Locale

### Prérequis techniques
- **Python** : Version 3.12 ou supérieure
- **PostgreSQL** : Version 16+
- **Redis** : Version 7+
- **Docker & Docker Compose** (pour l'infrastructure locale et le déploiement)
- **Compte Telegram** avec bot créé via [@BotFather](https://t.me/BotFather)

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

---

## 10. Configuration des Variables d'Environnement (.env)

Les paramètres applicatifs sont déclarés et typés dans [`app/core/config.py`](app/core/config.py) avec le modèle Pydantic Settings. Ce tableau résume les paramètres usuels ; le schéma et `.env.example` restent les références :

| Variable | Type / Format | Requis en Prod | Description |
|---|---|:---:|---|
| `APP_ENV` | `development` / `production` | Oui | Environnement d'exécution. Active le verrou de sécurité strict en production. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` | Non | Niveau de verbosité des logs (par défaut `INFO`). |
| `DATABASE_URL` | URI `postgresql+asyncpg://...` | Oui | Connexion PostgreSQL asynchrone pour SQLAlchemy 2.0. |
| `REDIS_URL` | URI `redis://...` | Oui | Connexion Redis pour le stockage FSM aiogram et le cache de session. |
| `TELEGRAM_BOT_TOKEN` | Chaîne `123456:ABC...` | **Oui** | Jeton d'API Telegram remis par @BotFather. |
| `TELEGRAM_ALLOWED_USER_IDS` | IDs séparés par virgules | **Oui** | Allowlist des identifiants numériques Telegram autorisés à interagir. |
| `TELEGRAM_WEBHOOK_SECRET` | Chaîne aléatoire (32+ chars) | **Oui** | Secret de sécurisation du Webhook HTTPS Telegram. |
| `INGEST_API_TOKEN` | Jeton d'authentification Bearer | **Oui** | Secret d'accès à l'API d'ingestion `/api/v1/ingest/listings`. |
| `INTERNAL_API_TOKEN` | Jeton d'authentification Bearer | **Oui** | Secret d'accès aux déclenchements internes `/api/v1/internal/...`. |
| `RADAR_API_URL` | URL HTTP (défaut : `http://127.0.0.1:8000`) | Non | URL locale de l'API utilisée pour les flux internes de détection. |
| `BOTRADAR_API_URL` | URL HTTP | Non | Alias optionnel prioritaire sur `RADAR_API_URL`. |
| `AI_PROVIDER` | `openai-compatible` / `anthropic` | Non | Adaptateur de moteur IA (défaut : `openai-compatible`). |
| `AI_API_KEY` | Chaîne `sk-...` | Non | Clé API du moteur IA, quel qu'en soit le fournisseur. |
| `AI_BASE_URL` | URL HTTP | Non | Base URL du fournisseur (défaut : `https://api.deepseek.com/v1`). |
| `AI_MODEL` | Nom du modèle | Non | Modèle utilisé (défaut : `deepseek-flash`). |
| `AI_TIMEOUT_SECONDS` | Durée en secondes | Non | Délai d'attente d'un appel au moteur (défaut : `12.0`). |
| `AI_ENABLED` | `true` / `false` | Non | Active ou désactive l'ensemble du sous-système IA. |
| `AI_FEATURE_*` | `true` / `false` | Non | Activation unitaire des 5 modules (`NATURAL_ADD`, `PROFIT_SCORING`, `NEGOTIATION`, `KEYWORD_VARIANTS`, `AUTO_TUNING`). |
| `EBAY_CLIENT_ID` / `_SECRET` | Chaînes OAuth2 | Non | Identifiants eBay Developer pour la consultation des cotes BD. |
| `EBAY_ENV` | `PRODUCTION` / `SANDBOX` | Non | Environnement de l'API eBay Browse (défaut : `PRODUCTION`). |
| `CHASSE_AUX_LIVRES_DUIH` | Chaîne de session | Non | Session utilisateur pour l'historique de cotes BD 1 an. |
| `VINTED_POLLER_ENABLED` | `true` / `false` | Non | Active la boucle de détection Vinted dans le conteneur applicatif. |
| `VINTED_POLL_INTERVAL_SECONDS` | Entier (défaut `12`) | Non | Cadence de scrutation standard ; sert aussi de plafond aux cadences accélérées (pointe, proxy). |
| `VINTED_PROXY_URL` | URL HTTP/SOCKS5 | Non | Proxy résidentiel optionnel pour accélérer la scrutation à 8s. |
| `NIGHT_MODE_ENABLED` | `true` / `false` | Non | Suspension nocturne pour préserver la réputation IP (défaut : `true`). |
| `NIGHT_MODE_START_HOUR` | Entier (défaut `0`) | Non | Heure de début de la plage de nuit (00:00). |
| `NIGHT_MODE_END_HOUR` | Entier (défaut `7`) | Non | Heure de fin de la plage de nuit (07:00). |
| `NIGHT_MODE_TIMEZONE` | Fuseau horaire (défaut `Europe/Paris`) | Non | Fuseau horaire de référence pour le mode nuit. |
| `GOOGLE_BOOKS_API_KEY` | Clé API optionnelle | Non | Troisième autorité bibliographique après BNF et OpenLibrary ; absente, aucun appel à cette source. |
| `PRICE_REFRESH_TIMES` | Heures HH:MM (ex: `07:00,19:00`) | Non | Horaires de rafraîchissement complet des cotes du catalogue BD. |

> **Garde-fous stricts** :
> - En mode `APP_ENV=production`, `validate_production_secrets()` refuse le démarrage avant les producteurs si l'un des 4 secrets d'accès (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `INGEST_API_TOKEN`, `INTERNAL_API_TOKEN`) est vide.
> - Écart ouvert C11 : la configuration des commandes Telegram précède encore cette validation dans le lifespan. L’alignement documentaire ne corrige pas cet ordre.
> - `TELEGRAM_WEBHOOK_URL` et `EBAY_ENVIRONMENT` ne sont **pas** lus par l'application (l'environnement eBay est lu via `EBAY_ENV`).

Les substitutions `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` relèvent de
Compose, pas de Settings. `DOMAIN_NAME` n’est lu par aucun consommateur du dépôt.
Les scripts ont leurs options propres : par exemple `DEPLOY_HOST`, `DEPLOY_KEY`,
`DEPLOY_REMOTE_DIR` pour le déploiement, `DEPLOY_DIR` pour le pré-vol, et les
variables de compte décrites dans Subjects pour le renouvellement Chasse.
L’environnement d’un script ne constitue pas automatiquement un paramètre runtime.

---

## 11. Commandes Utiles (Makefile)

Le [`Makefile`](Makefile) regroupe les actions récurrentes d'exploitation et de test :

```bash
make help                 # Affiche l'inventaire des commandes disponibles
make dev                  # Démarre l'application locale avec rechargement à chaud
make test                 # Exécute la suite complète pytest
make bench                # Benchmarks locaux, sans preuve de livraison réelle
make lint                 # Contrôle la conformité du code avec Ruff et Mypy
make lock                 # Régénérer requirements.lock (après une modification de pyproject.toml)
make check-lock           # Vérifier que le verrou couvre toutes les dépendances déclarées
make check-history        # Rejoue Ruff et Pytest sur chaque commit de la branche
make pr                   # Crée la branche, commit, push et la PR vers main
make verify               # Second passage local (lint + tests) puis attente de la CI verte
make preflight            # Vérifie l'état du VPS avant une synchronisation
make verify-deploy        # Revérifie le VPS après une synchronisation
make deploy               # Déploiement complet, enchaîné, arrêt au premier échec
make format               # Corrige et formate automatiquement le code avec Ruff
make migrate              # Applique les révisions de base de données Alembic
make revision message="..." # Génère une nouvelle migration automatique
make up                   # Lance l'ensemble des conteneurs Docker
make down                 # Coupe les conteneurs Docker
make clean                # Supprime les répertoires de cache temporaires
```

---

## 12. Gestion des Migrations Alembic

Le schéma PostgreSQL est géré par Alembic. La tête du graphe inspecté est `0013_pause_actions`. Elle ne prouve pas la révision d’une base réelle. Les recettes spécialisées 0010/0011 restent limitées à leurs transitions historiques.

```bash
# Appliquer les migrations
alembic upgrade head

# Vérifier la révision courante
alembic current

# Créer une nouvelle migration
alembic revision --autogenerate -m "nom_de_la_migration"
```

---

## 13. Déploiement en Production (VPS)

### Architecture d'hébergement
- **Hôte** : VPS Ubuntu dédié (`ubuntu@88.96.56.44`).
- **Passerelle centralisée** : Conteneur global `caddy_gateway` raccordé au réseau Docker partagé `web_gateway` (ADR-013).
- **Conteneurs applicatifs** : `radar_app`, `radar_postgres`, `radar_redis`.
- **Répertoire de travail** : `~/bot-radar/`.
- **Domaine public** : `https://radar.aminezegmou.fr`.

### Passerelle centralisée (`~/gateway/`, hors dépôt)

La passerelle Caddy est partagée avec **BotStream** : son `Caddyfile` n'est pas versionné ici et ne doit pas être modifié à l'aveugle. Trois points vérifiés en production le 03/10/2026 :

- **Le plafond de payload est appliqué par l'application, pas par la passerelle.** `PayloadSizeLimitMiddleware` borne à 512 Ko et refuse les corps `Transfer-Encoding: chunked`. La directive `request_body { max_size }` de Caddy **n'intercepte pas** un corps streamé par `reverse_proxy` : testé avec un plafond de 10 Ko face à un corps de 100 Ko, la requête a atteint l'application. Ne pas le remettre en croyant qu'il protège.
- **Recharger exige l'adresse explicite.** L'API d'administration n'écoute que sur `127.0.0.1:2019` (pas sur `localhost`). Un `caddy reload` sans `--address` sort sans erreur et **n'applique rien** :

  ```bash
  cd ~/gateway
  docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile \
    --address 127.0.0.1:2019
  # vérifier que la config en service est bien celle qu'on croit :
  docker compose exec -T caddy wget -qO- http://127.0.0.1:2019/config/ | head -c 300
  ```

  Si le rechargement reste muet, `docker compose restart caddy` repart d'un état connu : le fichier est monté en lecture seule, donc relu au démarrage.

### Procédure de déploiement

La référence opérationnelle est [AGENTS.md](AGENTS.md#déploiement-production),
avec l’ordre exécuté par `scripts/deploy.py` : pré-vol → arrêt confirmé →
synchronisation → construction de l’image cible → migration ponctuelle et lecture
de révision → démarrage → santé et contrôles stricts.

```bash
make deploy
make deploy DEPLOY_ARGS=--dry-run  # Relire uniquement l’ordre
```

Un push ne déploie rien. Ne pas démarrer la cible avant sa migration ; une
interruption exige de constater code, conteneur et schéma avant reprise, sans
restauration automatique. Les recettes historiques des documents spécialisés
conservent leur schéma et leurs restrictions, elles ne remplacent pas cette procédure.

`/health` n'est pas un contrôle de disponibilité métier : un HTTP 200 ne prouve
ni la collecte Vinted, ni une écriture SQL, ni une réception Telegram. `/status`
distingue un scanner arrêté, inconnu ou en retard ; un état absent n'est pas actif.
La sonde IA atteste une réponse HTTP (avec cache court), pas une génération
réussie ni la validité d'une réponse JSON métier.

La confirmation d'un brouillon de création est sérialisée dans le processus
courant : identité et état sont relus sous protection jusqu'au commit et à la
fermeture du brouillon. Les autres contextes FSM progressent indépendamment ;
préparation BD et présentation se déroulent ensuite sans ce verrou. Un retour
de commit échoué laisse le résultat inconnu et interdit de réutiliser ce
brouillon. Cette protection locale ne constitue pas une garantie « exactement
une fois » après crash, perte du stockage FSM ou avec plusieurs processus.


### Reprise bornée des notifications (schéma `0011_notification_state`)

Une notification est suivie par couple annonce–alerte, avec sa destination
Telegram réservée. Plusieurs alertes d'un utilisateur restent des notifications
distinctes. `notified_at` est un marqueur d'accusé exploitable persisté, jamais une
preuve de lecture par l'utilisateur. Les anciens marqueurs restent inchangés ; les
anciens pending deviennent `unknown`, sans reconstruire leurs compteurs ou reçus.

| État persisté | Traitement |
| --- | --- |
| `ready` | Non-envoi ou refus récupérable établi ; attendre l'échéance. |
| `sending` | Réservation commitée avant l'appel. Sous le gate commun, un propriétaire actif termine avant toute relecture de reprise. Sans propriétaire et sans reçu conservé, l'état devient `unknown`. |
| `notified` | Marquage durable ; aucun nouvel envoi. |
| `unknown` | Aucun renvoi automatique, aucune déclaration de livraison, aucune expiration en « non envoyé ». Un reçu complet conservé permet uniquement de finir le marquage SQL. |
| `deferred` | Admission momentanément refusée (pause, préparation, autorisation) ; relire avant un nouvel envoi. |
| `abandoned` | Échec certain permanent ou budget/âge dépassé ; conserver le motif, sans reprise automatique. |

Les catégories du sender (`not_sent`, `rejected`, `acknowledged`, `unknown`) sont
ses observations transitoires, pas les états de la base. `not_sent` désigne un
échec local avant tout appel ; un timeout, un décodage invalide ou un reçu
inexploitable désignent `unknown`. Un refus photo reconnu autorise deux appels
photo au plus puis un repli texte ; un inconnu arrête immédiatement les replis.

Le job du scheduler existant passe chaque minute : dix destinataires au plus,
traités séquentiellement, sans nouvelle admission après vingt secondes.
Un appel déjà réservé et son nettoyage SQL peuvent terminer plus tard. Trois
cycles réservés maximum (premier compris), chacun de trois appels Telegram au
plus : neuf appels possibles au total, pas neuf livraisons. Timeout de dix
secondes par appel ; refus récupérable différé d'une minute, puis cinq minutes,
sans raccourcir `retry_after`. La limite d'âge est trente minutes depuis la
réception persistée initiale ; ni recollecte ni tentative ne la rajeunit.

La reprise charge les matches et l'annonce persistés, sans recollecter ni créer
un nouveau match. API, poller et reprise partagent le gate local de l'annonce.
Une association absente sur une annonce déjà ingérée ne constitue pas une preuve
de non-envoi : l'ingestion la classe inconnue, sans inventer de reçu. Matching,
alerte/préparation, autorisation, destination, échéance, budget et âge sont relus
avant réservation. Une destination déjà tentée ne peut pas être remplacée.

La purge automatique conserve les alertes supprimées portant encore des matches
non marqués ; la suppression utilisateur explicite garde son contrat de purge par
cascade. Une preuve effacée par cette suppression n'est pas reconstruite. Aucun
nouveau worker, verrou distribué, interface ni politique de conservation annuelle.
Cette protection est limitée au processus unique ; crash et perte des preuves
peuvent laisser des notifications inconnues, y compris des notifications non
parties. Elle ne promet ni récupération de tous les anciens pending ni livraison
« exactement une fois ».

**Livraison proposée, non exécutée localement :** utiliser la procédure ordonnée
existante (pré-vol, arrêt confirmé de l'application, synchronisation de la cible,
construction de l'image cible, migration depuis cette image sans services,
lecture de la révision, démarrage cible puis contrôles). La révision attendue pour
ce lot est `0011_notification_state`, après `0010_alert_preparation`.
Le nouveau code ne fonctionne pas sur l'ancien schéma. Après migration, l'ancien
code peut ignorer `unknown` et renvoyer des messages : le redémarrer n'est pas un
rollback sûr. En cas d'échec, conserver l'application arrêtée et les données ;
choisir une réparation compatible avec ce protocole ou une restauration
explicitement validée. Le downgrade destructeur de cette migration est refusé.
Les index sont créés dans la transaction de migration ; durée et volume de
production restent non mesurés. Ne pas lancer plusieurs versions/processus en
parallèle, ni appliquer cette migration en production dans ce mandat.

---

## 14. Exemples d'Utilisation API & Webhook

### Activation du Webhook Telegram

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://radar.aminezegmou.fr/api/v1/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>",
    "secret_token": "<TELEGRAM_WEBHOOK_SECRET>",
    "allowed_updates": ["message", "callback_query"]
  }'
```

### Ingestion d'une annonce (Webhook Marketplace / Scraper)

`isbn13` est une déclaration vendeur, pas une identité confirmée. Les séparateurs
sont normalisés et les ISBN-10 valides convertis en ISBN-13. Un code inutilisable
est écarté sans rejeter l'annonce : la réponse ajoute
`"warnings": ["listing_isbn_invalid_ignored"]` (sinon `[]`), avec un diagnostic
structuré dans les logs. Les autres validations restent applicables.
Le code brut reste dans `raw_payload` ; il ne crée aucune fiche de référence.
La confirmation bibliographique et la vérification de cohérence interviennent
après les envois Telegram, puis seulement la liaison et la cotation. Une référence
déjà portée par l'alerte conserve sa cote locale ; l'ISBN vendeur seul ne la fournit pas.
La saisie volontaire en édition d'alerte (ADR-038) conserve son comportement.

```bash
curl -X POST "https://radar.aminezegmou.fr/api/v1/ingest/listings" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <INGEST_API_TOKEN>" \
  -d '{
    "source_name": "vinted",
    "external_id": "4589201938",
    "url": "https://www.vinted.fr/items/4589201938-blacksad-tome-1-quelque-part-entre-les-ombres",
    "title": "Blacksad Tome 1 Quelque part entre les ombres EO",
    "description": "Très bon état général, édition originale grand format cartonné.",
    "category_slug": "bd_comics",
    "item_price_cents": 850,
    "estimated_protection_cents": 113,
    "estimated_shipping_cents": 288,
    "condition": "very_good",
    "seller_name": "bd_collectionneur",
    "seller_rating": 4.95,
    "photos": ["https://images1.vinted.net/t/01_023a1_cover.jpeg"],
    "isbn13": "9782205049657",
    "published_at": "2026-09-27T12:00:00Z"
  }'
```

**Réponse HTTP 200 OK (`ListingIngestResponse`)** :
```json
{
  "listing_id": "8fa538e1-5120-4e50-9bb4-bfe24e754a91",
  "is_duplicate": false,
  "estimated_total_cents": 1251,
  "matches": [
    {
      "alert_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "alert_name": "Blacksad EO",
      "notification_sent": true
    }
  ],
  "needs_retry": false,
  "warnings": []
}
```

### Endpoints internes sécurisés (`INTERNAL_API_TOKEN`)

```bash
# Consulter le cache de cote marché d'une BD par son ISBN-13
curl -X GET "https://radar.aminezegmou.fr/api/v1/internal/books/9782205049657/market-price" \
  -H "Authorization: Bearer <INTERNAL_API_TOKEN>"

# Consulter la liste des alertes actives
curl -X GET "https://radar.aminezegmou.fr/api/v1/internal/alerts" \
  -H "Authorization: Bearer <INTERNAL_API_TOKEN>"

# Vérifier le statut de la session Vinted
curl -X GET "https://radar.aminezegmou.fr/api/v1/internal/vinted/session" \
  -H "Authorization: Bearer <INTERNAL_API_TOKEN>"
```

---

## 15. Diagnostic des Erreurs Courantes

### 1. `TokenValidationError` ou refus de démarrage au boot
- **Cause** : Secret obligatoire manquant ou format incorrect (`<chiffres>:<chaîne>` pour Telegram).
- **Vérification** : En mode `APP_ENV=production`, `validate_production_secrets()` bloque l'app si l'un des tokens requis est vide.

### 2. Le bot Telegram ne répond pas
- **Vérifications** :
  - Votre compte Telegram est-il bien renseigné dans `TELEGRAM_ALLOWED_USER_IDS` ?
  - L'URL du webhook est-elle joignable et terminée par `/<TELEGRAM_WEBHOOK_SECRET>` ?
  - Consultez les logs applicatifs : `docker compose logs -f app`.

### 3. Erreur 401 sur l'ingestion (`/api/v1/ingest/listings`)
- **Cause** : Le jeton passé dans `Authorization: Bearer <token>` ne correspond pas à la valeur de `INGEST_API_TOKEN` définie dans `.env`.

### 4. L'annonce BD arrive sans comparateur de prix de marché
- **Causes normales (Invariant n°1)** :
  - L'annonce ne contient aucun ISBN et aucun lien direct n'a pu être établi avec certitude avec le catalogue local.
  - Le livre vient d'être découvert : une résolution/cotation de fond peut être tentée pour les prochaines annonces, sans garantie de notice ni d’offre. Le message déjà envoyé décrit l’état à son envoi.
  - Aucun stock d'occasion livrée n'a été trouvé chez les fournisseurs.
- **Vérification du cache** : Vous pouvez consulter directement l'état de la cote en cache avec :
  `curl -X GET "https://radar.aminezegmou.fr/api/v1/internal/books/<ISBN13>/market-price" -H "Authorization: Bearer <INTERNAL_API_TOKEN>"`.

---

## 16. Sécurité & Confidentialité des Données

- **Principe du moindre privilège** : L'image Docker s'exécute avec un utilisateur système non root dédié (`appuser:1001`).
- **Isolation réseau stricte** : Compose publie PostgreSQL et Redis sur le loopback de l’hôte ; les conteneurs les joignent par le réseau Docker interne. Cette configuration déclarative n’atteste pas celle d’une installation réelle.
- **Masquage proactif des secrets** : Les jetons et clés d'API sont automatiquement masqués des logs grâce à `JSONFormatter` et encapsulés dans le type `SecretStr` de Pydantic v2.
- **Zéro automatisation d'achat intrusif** : Le bot ne déclenche ni achat, ni panier, ni message vendeur. Cela ne garantit pas l’absence de blocage de la collecte ou de bannissement.

---

_BotRadar — Conçu avec rigueur et précision pour des acquisitions de seconde main instantanées et éclairées._ 🛰️

### Référence glissante documentée des prix

La comparaison privilégie le minimum des offres livrées comparables effectivement
conservées dans `T − 365 jours < observed_at ≤ T`, à l'instant UTC `T`. À défaut,
elle utilise le prix courant encore admissible ; une offre périmée reste seulement
informative. Les règles de montant, port, état, arrondi et seuil utilisateur sont
inchangées : perdre une cote ne rejette pas une annonce conforme à ce seuil.

Le cache possède une projection annuelle distincte, recalculée en SQL local groupé
à échéance, sans appel fournisseur. Une projection expirée n'est jamais admissible.
Les minima ex æquo privilégient le relevé le plus récent, puis l'identifiant le plus
petit. La fenêtre annuelle reste partielle — une durée d'un an n'atteste aucune
observation continue — et les sommes de références individuelles ne prétendent pas
avoir été observées simultanément.

Les anciens minima et les agrégats sans date du minimum sont conservés comme
informations non vérifiables, hors calcul annuel. Les snapshots anciens admissibles
utilisent leur date locale d'enregistrement originale ; aucune date fournisseur
n'est inventée. Les observations nouvelles portent une date UTC et un identifiant
stables à travers rollback et nouvelle sauvegarde. Les observations financières
normalisées admissibles sont conservées 366 jours (marge technique hors fenêtre),
les payloads bruts 90 jours ; les autres rétentions restent inchangées. Les périodes
déjà purgées ne sont pas reconstruites.

La migration additive `0012_annual_observations`, après `0011_notification_state`,
laisse les nouvelles preuves à NULL et conserve les colonnes antérieures. Le retour
à un ancien code pouvant comparer les minima non vérifiables exige une décision
explicite ; le downgrade automatique est refusé.

### Sauvegarde et restauration synthétiques

L'[outillage du point 9](docs/backup-recovery.md) vérifie un dump custom PostgreSQL,
un chiffrement à clé publique, une publication locale simulée et une restauration
vide avec quarantaine durable. Il ne configure aucune sauvegarde indépendante et
n'active aucune planification ou rétention. L'ancien appel sans paramètres de
`scripts/backup_db.sh` échoue explicitement ; `--synthetic-exercise` lance uniquement
l'exercice jetable. Le document détaille les objectifs UTC, les limites et les
prérequis opérationnels avant tout remplacement de la sauvegarde réelle.
