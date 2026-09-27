# BotRadar 🛰️

[![CI](https://github.com/ZeAmine/radar-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ZeAmine/radar-bot/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/Tests-344%20passed-success?style=flat&logo=pytest)](https://docs.pytest.org/)
[![Linter](https://img.shields.io/badge/Linter-Ruff%20strict-black?style=flat&logo=ruff)](https://astral.sh/ruff)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat&logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![aiogram](https://img.shields.io/badge/aiogram-3.13+-2CA5E0?style=flat&logo=telegram)](https://docs.aiogram.dev/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0%20Async-D71F00?style=flat&logo=sqlalchemy)](https://www.sqlalchemy.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=flat&logo=postgresql)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=flat&logo=redis)](https://redis.io/)
[![DeepSeek](https://img.shields.io/badge/AI-DeepSeek--V4.1--Flash-blueviolet?style=flat)](https://www.deepseek.com/)
[![Architecture](https://img.shields.io/badge/Architecture-Single--Process%20Monolith-blue?style=flat)](AGENTS.md)

**BotRadar** est un bot Telegram **privé**, temps réel et hautement disponible, conçu comme un assistant d'aide à la décision pour l'achat de produits de seconde main.

Il surveille quatre univers à forte valeur ajoutée :
- 📚 **BD & Comics / Mangas** (`bd_comics`) — Cotes du marché d'occasion livrée, gestion des tomes et éditions originales.
- 💻 **Tech & Informatique** (`tech`) — Détection de matériel avec exclusion automatique de composants HS ou pièces détachées.
- 🎮 **Jeux vidéo & Consoles** (`gaming`) — Filtrage strict des éditions PAL-FR (rejet des jaquettes étrangères ES/DE/IT sans VF).
- 👕 **Vêtements & Mode** (`clothes`) — Filtrage granulaire des marques, tailles, états et détection des contrefaçons.

---

## Sommaire

1. [Philosophie & Positionnement Produit](#1-philosophie--positionnement-produit)
2. [Architecture Technique & Invariants](#2-architecture-technique--invariants)
3. [Clavier & Expérience Telegram](#3-clavier--expérience-telegram)
4. [Moteur de Matching Déterministe CPU](#4-moteur-de-matching-déterministe-cpu)
5. [Sous-système IA DeepSeek-V4.1-Flash](#5-sous-système-ia-deepseek-v41-flash)
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
- ⚡ **Détecteur instantané** : Ingestion et notification en **< 500 ms** dès publication d'une annonce.
- 📊 **Comparateur de marché réel** : Calcul synchrone de la cote d'occasion livrée (eBay, Momox, Gibert, Recyclivre, Chasse-aux-livres).
- 🧠 **Copilote décisionnel et d'auto-apprentissage** : Estimation de revente, coach de négociation et affinage automatique des alertes via IA.
- 🔒 **Système strictement privé** : Mono-utilisateur avec filtrage par allowlist (`TELEGRAM_ALLOWED_USER_IDS`).

### Ce que BotRadar N'EST PAS (et ne sera jamais)
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
│          │                       ├────────► [ ⚡ ALERTE TELEGRAM < 500ms ]  │
│          │                       │                │                         │
│          │                       ▼                ▼ (safe_create_task)      │
│          │                  Cote BD Locale    IA DeepSeek / BNF             │
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

1. **Invariant n°1 — L'alerte Telegram d'abord (< 500 ms)** :
   - Dédoublonnage instantané `(source_name, external_id)`.
   - Matching en mémoire CPU locale (`evaluate_listing_against_alert`).
   - Cote BD : **cache local PostgreSQL uniquement** (`MarketPriceService.get_comparison_summary` est synchrone, zéro requête HTTP).
   - Envoi immédiat du message Telegram (`send_listing_alert`).
   - **En tâche de fond différée uniquement (`safe_create_task`)** : enrichissement IA DeepSeek avec le vrai `message_id` et association ISBN BNF dans une session BDD distincte (ADR-009).
   - *Interdiction absolue de bloquer l'alerte par un appel réseau externe (BNF, eBay, DeepSeek).*

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

```
┌──────────────────────────┬──────────────────────────┐
│        🖼 Photos         │        🔗 Annonce        │
├──────────────────────────┴──────────────────────────┤
│           📊 Prix du marché (BD uniquement)          │
├──────────────────────────┬──────────────────────────┤
│      👎 Faux positif     │        💬 Négocier       │
└──────────────────────────┴──────────────────────────┘
```

- **`[ 🖼 Photos ]`** : Affiche instantanément l'album complet en qualité HD (toutes les photos sont extraites dès le scan Vinted, zéro requête HTTP au moment du clic).
- **`[ 🔗 Annonce ]`** : Ouvre directement la page de l'article dans l'application Vinted avec les moyens de paiement et sessions de l'utilisateur.
- **`[ 📊 Prix du marché ]`** *(BD uniquement)* : Consulte le détail des offres d'occasion livrée (eBay, Momox, Gibert, Recyclivre, Chasse-aux-livres) et l'écart de prix.
- **`[ 👎 Faux positif ]`** : Enregistre le rejet de l'annonce pour alimenter le moteur d'apprentissage `/autotune`. Se transforme en `[ ✅ Signalé ]`.
- **`[ 💬 Négocier ]`** : Déclenche le coach IA DeepSeek pour obtenir une estimation d'offre pertinente et un message de négociation pré-rédigé courtois prêt à copier.

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
| `/status` | Diagnostic de santé du service, du poller Vinted et de l'IA DeepSeek |
| `/cancel` | Annulation immédiate de l'opération ou du dialogue FSM en cours (alias : `/annuler`) |
| `/help` | Affichage du guide d'utilisation interactif |

---

## 4. Moteur de Matching Déterministe CPU

Le moteur de matching ([`app/domain/rules/listing_match.py`](app/domain/rules/listing_match.py)) évalue chaque annonce en mémoire CPU en **moins de 2 millisecondes** :

1. **Normalisation Unicode NFKD** : Suppression des accents, uniformisation des minuscules, des apostrophes typographiques et des tirets.
2. **Dictionnaire d'exclusions multilingue** (`MULTILINGUAL_EXCLUSION_MAPPING`) : Traduction et expansion automatique des exclusions en espagnol, allemand, italien et anglais (`boite vide` ➔ `caja vacia`, `nur ovp`, `solo scatola`, etc.).
3. **Filtre qualité Gaming anti-imports** : Rejet automatique des jeux vidéo en langues étrangères (DE, ES, IT) sauf mention explicite d'une édition PAL-FR ou VF.
4. **Exclusions matérielles par défaut** : Protection native contre les goodies, boîtes vides, accessoires orphelins (câbles seuls, chargeurs seuls) et articles défectueux ("pour pièces", "HS").
5. **Protection hybride BD / ISBN** :
   - Si un ISBN-13 est présent sur l'annonce, vérification exacte.
   - En l'absence d'ISBN textuel, fallback déterministe sur le titre de la série, le numéro de tome et l'auteur pour garantir zéro faux positif sur les titres courts (*"Blast"*, *"RIP"*, *"1629"*).
6. **Requête groupée** : Une seule requête SQL pour récupérer les alertes candidates (`matched_alert_ids`), aucun parcours itératif en base de données.

---

## 5. Sous-système IA DeepSeek-V4.1-Flash

L'intelligence artificielle repose sur le modèle `deepseek-flash` via l'API OpenAI compatible. Elle intervient de manière **purement asynchrone post-alerte** et ne bloque jamais la vitesse d'envoi.

Cinq modules vivants et spécialisés sont actifs ([`app/services/ai/modules/`](app/services/ai/modules/)) :

1. 🪄 **`natural_add`** (`AI_FEATURE_NATURAL_ADD=true`) : Parse une phrase libre en langage naturel pour pré-remplir les filtres d'une alerte lors de `/add`.
2. 💎 **`profit_scoring`** (`AI_FEATURE_PROFIT_SCORING=true`) : Calcule un score pépite, estime le prix de revente potentiel et la marge nette pour la tech, le gaming et les vêtements.
3. 💬 **`negotiation`** (`AI_FEATURE_NEGOTIATION=true`) : Analyse l'écart de prix et rédige un message de négociation poli et personnalisé à destination du vendeur.
4. ✨ **`keyword_variants`** (`AI_FEATURE_KEYWORD_VARIANTS=true`) : Suggère des variantes orthographiques, abréviations et fautes courantes lors de la configuration d'une alerte (`[ ✨ Suggérer variantes IA ]`).
5. 🎯 **`auto_tuning`** (`AI_FEATURE_AUTO_TUNING=true`) : Analyse les faux positifs signalés par l'utilisateur (`[ 👎 Faux positif ]`) et génère des listes d'exclusions prêtes à copier ou modifier dans `/autotune` (ADR-008).

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

- 🚀 **API Interne JSON First (`/svc-catalogue/items`)** : Payload réduit de 7,25 Mo (HTML) à **~38 Ko** par requête (divisé par 190), avec latence d'interrogation chutant à **~200 ms**.
- 🛡️ **Repli Transparent (Fallback HTML)** : En cas d'anomalie sur le format JSON, le poller bascule automatiquement sur le catalogue HTML classique sans interruption.
- ⚡ **Cadence Adaptative** : Intervalle de scrutation de **12s** en heures de pointe (11h30-14h00 et 18h00-22h30 Paris), **15s** en heures standard, et passage automatique à **8s** dès configuration d'un proxy résidentiel (`VINTED_PROXY_URL`).
- 🌙 **Mode Nuit Automatique** : Suspension ou ralentissement de la scrutation entre 00:00 et 07:00 (Paris) pour préserver la réputation de l'IP.
- 🔀 **Ordonnancement Entrelacé (Staggered Interleaving)** : Espacement calculé (1,2s à 2,0s) entre les requêtes de différentes catégories pour lisser la charge réseau.
- 🖼️ **Extraction Multi-Photos HD** : Stockage direct de l'ensemble des photos de l'annonce lors du scan pour affichage immédiat dans Telegram.

---

## 8. Intégration Continue (CI) & Qualité de Code

Le projet est outillé avec un pipeline d'Intégration Continue via GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) qui s'exécute à chaque push et pull request :

- **Ruff** : Linting strict conforme aux règles modernes Python 3.12 (longueur de ligne max 100 caractères, imports ordonnés, conventions PEP).
- **Pytest & Pytest-Asyncio** : Suite complète de **344 tests automatisés** couvrant l'ingestion, le matching déterministe, le FSM Telegram, le cache de cotes, le poller Vinted et le sous-système IA.
- **Typage Statique (Mypy)** : Configuré en `strict = true` dans `pyproject.toml` (avec exemptions historiques isolées) et exécutable localement via `make lint`.

Pour exécuter les vérifications localement :

```bash
# Vérification du style et du linting
.venv/bin/ruff check .

# Exécution de la suite complète de tests (344 tests)
.venv/bin/pytest -v
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

1. **Cloner le dépôt** :
   ```bash
   git clone https://github.com/ZeAmine/radar-bot.git
   cd "radar-bot"
   ```

2. **Créer et activer le virtualenv Python 3.12** :
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -e ".[dev]"
   ```

3. **Démarrer les services de données locaux (Postgres & Redis)** :
   ```bash
   docker compose up -d postgres redis
   ```

4. **Configurer l'environnement** :
   ```bash
   cp .env.example .env
   # Éditez .env avec vos identifiants
   ```

5. **Appliquer les migrations de base de données** :
   ```bash
   alembic upgrade head
   ```

6. **Lancer le serveur de développement** :
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

---

## 10. Configuration des Variables d'Environnement (.env)

Toutes les variables sont déclarées et typées dans [`app/core/config.py`](app/core/config.py) avec modèle Pydantic Settings :

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
| `DEEPSEEK_API_KEY` | Chaîne `sk-...` | Non | Clé API pour le moteur IA DeepSeek-V4.1-Flash. |
| `DEEPSEEK_BASE_URL` | URL HTTP | Non | Base URL DeepSeek (défaut : `https://api.deepseek.com/v1`). |
| `DEEPSEEK_MODEL` | Nom du modèle | Non | Modèle utilisé (défaut : `deepseek-flash`). |
| `AI_ENABLED` | `true` / `false` | Non | Active ou désactive l'ensemble du sous-système IA. |
| `AI_FEATURE_*` | `true` / `false` | Non | Activation unitaire des 5 modules (`NATURAL_ADD`, `PROFIT_SCORING`, `NEGOTIATION`, `KEYWORD_VARIANTS`, `AUTO_TUNING`). |
| `EBAY_CLIENT_ID` / `_SECRET` | Chaînes OAuth2 | Non | Identifiants eBay Developer pour la consultation des cotes BD. |
| `EBAY_ENV` | `PRODUCTION` / `SANDBOX` | Non | Environnement de l'API eBay Browse (défaut : `PRODUCTION`). |
| `CHASSE_AUX_LIVRES_DUIH` | Chaîne de session | Non | Session utilisateur pour l'historique de cotes BD 1 an. |
| `VINTED_POLLER_ENABLED` | `true` / `false` | Non | Active la boucle de détection Vinted dans le conteneur applicatif. |
| `VINTED_POLL_INTERVAL_SECONDS` | Entier (défaut `15`) | Non | Fréquence de scrutation standard hors heures de pointe. |
| `VINTED_PROXY_URL` | URL HTTP/SOCKS5 | Non | Proxy résidentiel optionnel pour accélérer la scrutation à 8s. |
| `NIGHT_MODE_ENABLED` | `true` / `false` | Non | Suspension nocturne pour préserver la réputation IP (défaut : `true`). |
| `NIGHT_MODE_START_HOUR` | Entier (défaut `0`) | Non | Heure de début de la plage de nuit (00:00). |
| `NIGHT_MODE_END_HOUR` | Entier (défaut `7`) | Non | Heure de fin de la plage de nuit (07:00). |
| `NIGHT_MODE_TIMEZONE` | Fuseau horaire (défaut `Europe/Paris`) | Non | Fuseau horaire de référence pour le mode nuit. |
| `PRICE_REFRESH_TIMES` | Heures HH:MM (ex: `07:00,19:00`) | Non | Horaires de rafraîchissement complet des cotes du catalogue BD. |

> **Garde-fous stricts** :
> - En mode `APP_ENV=production`, `validate_production_secrets()` bloque immédiatement le démarrage si l'un des 4 secrets d'accès (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `INGEST_API_TOKEN`, `INTERNAL_API_TOKEN`) est vide.
> - `TELEGRAM_WEBHOOK_URL` et `EBAY_ENVIRONMENT` ne sont **pas** lus par l'application (l'environnement eBay est lu via `EBAY_ENV`).

---

## 11. Commandes Utiles (Makefile)

Le [`Makefile`](Makefile) regroupe les actions récurrentes d'exploitation et de test :

```bash
make help                 # Affiche l'inventaire des commandes disponibles
make dev                  # Démarre l'application locale avec rechargement à chaud
make test                 # Exécute la suite complète de 344 tests pytest
make lint                 # Contrôle la conformité du code avec Ruff et Mypy
make format               # Corrige et formate automatiquement le code avec Ruff
make migrate              # Applique les révisions de base de données Alembic
make revision message="..." # Génère une nouvelle migration automatique
make up                   # Lance l'ensemble des conteneurs Docker
make down                 # Coupe les conteneurs Docker
make clean                # Supprime les répertoires de cache temporaires
```

---

## 12. Gestion des Migrations Alembic

Le schéma PostgreSQL est géré par Alembic. La révision courante attendue est `0004_alert_books_book_id_index`.

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

### Procédure de déploiement

1. **Synchronisation des fichiers** (en préservant scrupuleusement le `.env` de production) :
   ```bash
   rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
     --exclude '.git' --exclude '.env' --exclude 'vendor' \
     -e "ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no" \
     ./ ubuntu@88.96.56.44:~/bot-radar/
   ```

2. **Rebuild et redémarrage du conteneur applicatif** :
   ```bash
   ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 \
     "cd ~/bot-radar && docker compose up -d --build app"
   ```

3. **Application des migrations de schéma** (une fois le conteneur healthy) :
   ```bash
   ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 \
     "cd ~/bot-radar && docker compose exec -T app alembic upgrade head && docker compose exec -T app alembic current"
   ```

4. **Contrôle d'intégrité opérationnel** :
   ```bash
   curl -s https://radar.aminezegmou.fr/health
   # Réponse attendue : {"status":"ok","environment":"production","ai":{"status":"operational",...}}
   ```

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
    "photos": [
      "https://images1.vinted.net/t/01_023a1_photo1.jpeg",
      "https://images1.vinted.net/t/01_023a1_photo2.jpeg"
    ],
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
  "needs_retry": false
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
  - Le livre vient d'être découvert : il a été programmé pour enrichissement BNF en arrière-plan, et sa cote sera disponible au prochain cycle.
  - Aucun stock d'occasion livrée n'a été trouvé chez les fournisseurs.
- **Vérification du cache** : Vous pouvez consulter directement l'état de la cote en cache avec :
  `curl -X GET "https://radar.aminezegmou.fr/api/v1/internal/books/<ISBN13>/market-price" -H "Authorization: Bearer <INTERNAL_API_TOKEN>"`.

---

## 16. Sécurité & Confidentialité des Données

- **Principe du moindre privilège** : L'image Docker s'exécute avec un utilisateur système non root dédié (`appuser:1001`).
- **Isolation réseau stricte** : PostgreSQL et Redis ne sont pas exposés sur l'Internet public et communiquent uniquement au sein du réseau privé interne Docker.
- **Masquage proactif des secrets** : Les jetons et clés d'API sont automatiquement masqués des logs grâce à `JSONFormatter` et encapsulés dans le type `SecretStr` de Pydantic v2.
- **Zéro automatisation d'achat intrusif** : Respect scrupuleux des conditions d'utilisation des plateformes de seconde main, éliminant tout risque de bannissement ou d'action marketplace non sollicitée.

---

_BotRadar — Conçu avec rigueur et précision pour des acquisitions de seconde main instantanées et éclairées._ 🛰️
