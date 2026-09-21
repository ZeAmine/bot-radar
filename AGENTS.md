# Radar Bot — instructions projet

Ce fichier est la source de vérité pour tout assistant qui travaille sur le dépôt. Le lire avant de modifier du code, de déployer, ou de proposer une architecture.

## Produit

Radar Bot est un bot Telegram **privé** d’aide à la décision pour l’occasion (BD/comics, vêtements, tech, jeux vidéo).

- Il **détecte** des annonces Vinted et envoie une alerte Telegram instantanée.
- Il **compare** les BD au marché d’occasion livrée (cache Postgres, refresh planifié).
- Il **n’achète pas**, n’ajoute pas au panier, n’envoie pas de message vendeur, n’automatise aucune action marketplace.

Utilisateur unique / allowlist (`TELEGRAM_ALLOWED_USER_IDS`). Domaine public : `https://radar.aminezegmou.fr`.

## Stack

- Python 3.12, FastAPI, aiogram 3, SQLAlchemy asyncio, Alembic, Redis, APScheduler, httpx, Pydantic v2.
- Un seul processus de production : `uvicorn app.main:app` dans `radar_app` (API + webhook Telegram + scheduler + poller Vinted).
- PostgreSQL 16, Redis 7, Caddy (HTTPS). Compose : [compose.yml](compose.yml).
- Tests : pytest + pytest-asyncio. Lint : Ruff (ligne 100). Typage : mypy `strict` (erreurs historiques hors lots en cours, ne pas les « corriger » en masse).

## Architecture des couches

Sens autorisé des dépendances :

```
api / bot  →  use_cases  →  domain
                ↓
         services / providers / db
```

| Couche | Rôle | Interdit |
| --- | --- | --- |
| `app/api` | HTTP, auth Bearer, conversion vers use case | SQL métier, appels DeepSeek hors `/health` |
| `app/bot` | Handlers, FSM, claviers, HTML Telegram | Nouvelle logique métier lourde ; garder les handlers existants tant qu’on n’extrait pas volontairement |
| `app/use_cases` | Orchestration d’un flux | Appels réseau lents **avant** l’envoi Telegram |
| `app/domain` | Entités, protocoles, règles pures | Importer `app.services` dans du **nouveau** code. L’inversion actuelle (`domain/rules/matcher.py` → `matcher_service`) est de la dette : ne pas l’aggraver |
| `app/db` | Modèles, sessions, repositories | Règles de matching ou HTML Telegram |
| `app/providers` | HTTP externe (timeout, parse) | Écriture Postgres |
| `app/services` | Métier / orchestration limitée | Bloquer le chemin d’alerte |
| `app/tasks` | Scheduler et poller | Lancer un second processus scheduler |
| `app/core` | Settings, logs, Redis, HTTP partagé, sécurité | Enums de présentation bot (labels, emojis) |
| `app/utils` | Helpers purs (`text.py`, `isbn.py`) | I/O |

Ne pas inventer une nouvelle couche. Ne pas déplacer `tome_extractor.py` ni fusionner les enums `app/core/constants.py` / `app/domain/enums.py` sans demande explicite (churn d’imports, aucun gain runtime).

## Invariant n°1 — l’alerte Telegram d’abord

Toute modification du chemin ingestion → matching → Telegram doit respecter cet ordre :

1. Dédoublonner l’annonce (`source_name` + `external_id`).
2. Matcher en CPU local (`evaluate_listing_against_alert`). Une requête groupée `matched_alert_ids` par annonce, pas un `SELECT` par alerte.
3. Cote BD : **cache local uniquement**. `MarketPriceService.get_comparison_summary` est synchrone, zéro HTTP.
4. `send_listing_alert` — priorité absolue.
5. Ensuite seulement, en `safe_create_task` :
   - enrichissement DeepSeek dans `telegram_service.py` avec le **vrai** `message_id` ;
   - liaison ISBN BNF (`_link_book_from_bnf`) dans une **autre** session.

Interdit sur ce chemin :

- `await` vers BNF, eBay, Momox, Gibert, Recyclivre, Chasse-aux-livres, DeepSeek **avant** l’envoi.
- Relancer `AsyncEnrichListingUseCase.schedule_enrichment` (supprimé : il partait avec `message_id=0`).
- Un second client HTTP DeepSeek « pour voir ».
- `time.sleep` dans `app/`.
- Calculs lourds ou regex globaux « optimisés » sans benchmark reproductible.

Conséquence assumée : la **première** alerte d’une BD inconnue peut partir sans ligne de cote. L’ISBN BNF sert aux suivantes.

## Invariant n°2 — un seul scheduler

- `app/main.py` démarre **toujours** APScheduler. Le poller Vinted est optionnel (`VINTED_POLLER_ENABLED`).
- Jobs : `max_instances=1`, `coalesce=True` (07:00, 19:00 Europe/Paris + cleanup 03:30).
- **Ne jamais** lancer `python -m app.worker` (ou `make worker`) en même temps que `radar_app`. Le worker le logue en `WARNING`. Compose n’a pas de service worker.
- Pas de verrou Redis tant qu’il n’y a qu’un processus. Ne pas en ajouter « au cas où ».

## Invariant n°3 — secrets et logs

- Tokens d’accès : `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `INGEST_API_TOKEN`, `INTERNAL_API_TOKEN`.
- En `APP_ENV=production`, `validate_production_secrets()` refuse de démarrer si l’un est vide. Ne pas contourner.
- Ne jamais logger un secret. `JSONFormatter` masque Bearer, `token=` / `secret=` / `password=` / `key=`, préfixe `sk-`, et les extras dont la clé est sensible. Garder ce comportement.
- `compare_digest` : un secret attendu **vide** et un secret reçu vide matchent. D’où le garde-fou de démarrage.
- `.env` local et distant : ne pas les committer, ne pas les coller dans le chat, ne pas les rsyncer depuis un exemple qui écraserait la prod.
- Clés lues par l’app : uniquement [`.env.example`](.env.example) / [`app/core/config.py`](app/core/config.py). `TELEGRAM_WEBHOOK_URL` et `EBAY_ENVIRONMENT` ne sont **pas** lus. eBay : `EBAY_ENV`.
- OpenAPI (`/docs`, `/redoc`, `/openapi.json`) est coupé seulement si `APP_ENV=production`.

## Qualité de code

1. **Zéro code mort** : pas de blocs commentés, pas de flags sans module, pas de second chemin « au cas où » à côté du chemin réel.
2. Remplacer ou améliorer le code existant ; ne pas ajouter un service parallèle qui duplique un use case.
3. Français pour les messages utilisateur et les docstrings métier ; identifiants Python en anglais.
4. `safe_create_task` pour tout arrière-plan asyncio (référence forte anti-GC + log d’exception).
5. Client HTTP : `get_shared_http_client()`, pas un `httpx.AsyncClient()` par requête.
6. Redis : pool `app.core.redis.get_redis_pool`. Ne pas réintroduire l’import depuis `app.api.dependencies`.
7. Ne pas élargir le périmètre d’un correctif (pas de refactor opportuniste des handlers 900 lignes).
8. Après une modification : Ruff sur les fichiers touchés, puis les tests concernés. Suite complète avant un déploiement.

## Tests

- Ne supprimer aucun test existant. Adapter si le symbole testé disparaît volontairement.
- Le matching, l’idempotence d’ingestion, le calcul de total, le cache marché et le webhook secret sont des filets critiques.
- Les tests d’ingest patchent l’envoi via un réexport / `sys.modules` : préserver `_get_send_listing_alert()` tant que ce contrat tient.
- Pas de base globale partagée non isolée à introduire dans `conftest.py`.

## Fichiers sensibles — ne pas étendre, ne pas « améliorer » le comportement

Sans demande **explicite** de l’utilisateur :

- [`app/services/vinted_scraper.py`](app/services/vinted_scraper.py) — cookies Redis, Cloudflare, night mode.
- [`scripts/vinted_autobuy.user.js`](scripts/vinted_autobuy.user.js)
- [`scripts/sync_vinted_session.py`](scripts/sync_vinted_session.py)
- Formules de `price_calculator.py` / comparaison marché (une offre sans port ≠ total « article seul »).

Les scripts ne sont importés par `app/` qu’à dessein. `python-dotenv` sert à `scripts/local_vinted_poller.py`, pas au runtime Docker.

## Déploiement production

Hôte : `ubuntu@88.96.56.44`. Clé : `~/.ssh/ssh-key-2026-09-10.key`. Répertoire : `~/radar-bot/`.

Exécuter **soi-même** les commandes (réseau + clé SSH), ne pas demander à l’utilisateur de copier-coller.

Synchronisation (n’exclut pas `.env` distant : **ne pas** envoyer un `.env` local par accident ; le rsync ci-dessous ne cible pas `.env` s’il est absent du filtre, mais un `.env` local **écraserait** la prod s’il est présent — vérifier avant) :

```bash
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.git' --exclude '.env' \
  -e "ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no" \
  ./ ubuntu@88.96.56.44:~/radar-bot/
```

Rebuild de l’app (obligatoire pour `alembic/` : non monté en volume ; `./app` est bind-mounté) :

```bash
ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 \
  "cd ~/radar-bot && docker compose up -d --build app"
```

Migration **après** que le nouveau conteneur soit healthy :

```bash
ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 \
  "cd ~/radar-bot && docker compose exec -T app alembic upgrade head && docker compose exec -T app alembic current"
```

Contrôles : `radar_app` healthy, `https://radar.aminezegmou.fr/health` 200, pas de conteneur worker, pas de log `Secrets de production vides`, scheduler démarré. Ne pas dumper de tokens dans les logs collectés.

Ne pas committer ni pousser sauf demande. Ne pas `--force` git.

Révision Alembic actuelle attendue : `0004_alert_books_book_id_index` (`ix_alert_books_book_id`). Toute nouvelle migration : fichier dans `alembic/versions/`, rebuild, puis `upgrade head`. Pas d’`op.create_index` `CONCURRENTLY` dans une transaction Alembic standard ; table petite uniquement, sinon le demander.

## IA

Chemin **vivant** post-alerte : `app/services/ai/` (`DeepSeekClient` + `AIPostAlertPipeline.enrich_alert`). `data = None` avant l’appel si le client n’est pas configuré.

Chemin **tests / provider Pydantic + cache Redis** : `DeepSeekProvider` + `AsyncEnrichListingUseCase.execute`. Ne pas le recâbler sur l’ingestion. L’IA conseille, elle ne bloque jamais l’alerte.

Flags `AI_FEATURE_*` : un flag = un module dans `app/services/ai/modules/` (sauf valeurs off par défaut encore enregistrées : `price_glitch`, `bundle_decomposition`, `vision_guard`). Ne pas réintroduire un flag sans module.

## Dette volontairement reportée

Ne pas les traiter dans un correctif incident, sauf demande claire :

- Extraire SQL/IA des handlers (`alerts_create.py`, `alerts_manage.py`, `callbacks.py`, `internal.py`).
- Câbler `DeterministicListingMatcher` comme unique matcher (aujourd’hui le use case appelle le service ORM).
- Fusionner les deux piles DeepSeek.
- Fusionner les enums domaine / constants.
- Unifier les trois usages Redis (pool app, FSM aiogram, rate limiter).
- Validation Pydantic stricte des modules post-alerte (ils parsent encore en `.get()`).
- Index secondaire `(source_name, received_at)` sur `listings` : seulement si une requête prod le justifie.
