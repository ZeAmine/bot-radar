# Architecture et points d'entrée

Statut : contrat actuel. Périmètre : frontières des couches, démarrage et stockage.
Les invariants transversaux restent dans [AGENTS](../AGENTS.md).

Python 3.12, FastAPI, aiogram 3, SQLAlchemy asyncio, Alembic, Redis, APScheduler,
httpx et Pydantic v2. Un seul processus applicatif démarre dans `radar_app`.

| Couche | Responsabilité et frontière |
| --- | --- |
| `app/api`, `app/bot` | Transport HTTP/Telegram, auth, FSM et rendu ; pas de SQL métier ou d’appel IA dans l’API hors `/health`, ni nouvelle logique lourde dans les handlers |
| `app/use_cases` | Orchestration des flux vers domaine, services, providers et DB |
| `app/domain` | Entités, protocoles, règles pures ; aucun import de services |
| `app/services` | Métier et orchestration limitée ; ne pas bloquer l'alerte |
| `app/providers` | HTTP externe, délais et parsing ; aucune écriture PostgreSQL |
| `app/db` | Modèles, sessions et repositories ; ni matching ni HTML |
| `app/tasks` | Scheduler et poller du processus unique |
| `app/core` | Settings, logs, sécurité, HTTP/Redis partagés ; pas de labels Telegram |
| `app/utils` | Helpers purs, sans I/O |

Sens : API/bot → use cases → domaine ; les use cases utilisent services/providers/DB.
Les enums métier sont définis dans `app/domain/enums.py`, réexportés par
`app/core/constants.py`. `tome_extractor.py` reste dans les services.

## Entrées à lire

- [Lifespan](../app/main.py) : démarrage et arrêt coordonné des producteurs.
- [Ingestion](../app/use_cases/ingest_listing.py) : annonce → matching → notification.
- [Scheduler](../app/tasks/scheduler.py) et [poller](../app/tasks/vinted_poller.py) : producteurs.
- [Règles de matching](../app/domain/rules/listing_match.py) : calcul CPU ;
  `matcher_service` réexporte ce domaine.
- [Configuration](../app/core/config.py), [.env.example](../.env.example) et
  [Compose](../compose.yml) : sources des paramètres, services et ports.

PostgreSQL 16 porte alertes, annonces, relations et caches. Redis 7 porte FSM,
limitation et états temporaires. Aucune session SQLAlchemy ne traverse deux tâches
concurrentes : le fond ouvre sa propre session. Un résultat ORM d'une autre session
n'est pas attaché au handler ; relire les relations nécessaires dans sa session.

## Scheduler et schéma

APScheduler démarre même sans poller. Jobs sans chevauchement : `max_instances=1`,
`coalesce=True`. Une seule passe de prix, toutes les `PRICE_PASS_INTERVAL_MINUTES`
(quinze minutes par défaut), sans grille horaire ni mode ; bilan quotidien du marché
à 09:00 Europe/Paris ; cleanup à 03:30, auto-tuning à 10:00/14:00/18:00/22:00
Europe/Paris ; reprise BD au démarrage puis cinq minutes, reprise des notifications
trente secondes.
Les tests [scheduler](../tests/unit/test_scheduler_jobs.py) et
[lifespan](../tests/unit/test_unified_lifespan.py) couvrent des aspects locaux,
pas l'unicité effective d'un processus en production.

La tête du code est `0014_alert_market_freshness` ; ce n'est pas la révision d'une base réelle.
Une révision Alembic tient dans 32 caractères, contrôlée par
[test_alembic_revisions](../tests/unit/test_alembic_revisions.py).
Pas d'index `CONCURRENTLY` dans une transaction Alembic standard ; pour une grosse
table, arbitrage préalable. Rebuild puis `upgrade head` seulement dans un mandat
autorisant la migration. Aucun scheduler séparé ou verrou Redis préventif à ajouter.

La cotation planifiée couvre livres et tomes des alertes actives et en pause,
hors suppressions, avec déduplication. La passe unique reprend au démarrage, puis
sert les échéances les plus anciennes dans l'ordre chaud → tiède → froid, dans la
limite des places libres du régulateur : cible de réussite 24 h pour un livre chaud,
avertissement après 48 h de retard sur son rythme. Un échec ne déplace jamais
l'échéance — il la laisse due au rythme normal. La résolution quotidienne rattache
les séries et alertes sans lien sans déclencher de prix hors des vagues bornées.
Une panne externe persistante ne devient jamais une réussite pour satisfaire le délai.
