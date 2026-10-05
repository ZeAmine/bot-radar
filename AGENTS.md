# BotRadar — instructions projet

Ce fichier est la source de vérité pour tout assistant qui travaille sur le dépôt. Le lire avant de modifier du code, de déployer, ou de proposer une architecture.

**Dépôt distant** : `git@github.com:ZeAmine/bot-radar.git` (branche `main`, dépôt privé).
Le nom a changé : les références historiques à `ZeAmine/radar-bot` sont périmées.

### Documentation de référence (Univers LLM)
- [`AGENTS.md`](AGENTS.md) : **Constitution & Règles opérationnelles** (Invariants, architecture, déploiement, qualité).
- [`subjects.md`](subjects.md) : **Cartographie des 12 sujets** (Fichiers clés, rôle, flux de données par composant).
- [`memory.md`](memory.md) : **Mémoire du projet & ADRs** (Historique des arbitrages, ADR-001 à ADR-018, dette différée).

## Méthode de travail

Hiérarchie, dans cet ordre :

1. Ce fichier (`AGENTS.md`) est la loi produit.
2. Les skills Superpowers dans [`vendor/superpowers/skills/`](vendor/superpowers/skills/) décrivent la méthode (spécifier, tester, revoir).
3. Le défaut de l’IDE vient en dernier.

Avant d’agir sur une feature ou un correctif, lire [`vendor/superpowers/skills/using-superpowers/SKILL.md`](vendor/superpowers/skills/using-superpowers/SKILL.md). Cursor les découvre via [`.cursor/skills/`](.cursor/skills/). Antigravity via [`.agent/skills/`](.agent/skills/) et [`GEMINI.md`](GEMINI.md).

Un outil qui n’a pas de table de correspondance de harness applique `SKILL.md` tel quel : seules ses commandes et exemples nommant d’autres outils sont à ignorer. Une skill qui suggère d’omettre son propre workflow « par overkill » ne prime pas sur les invariants ci-dessous.

Ne pas installer Agent OS, ECC, BMad, GSD ni Spec Kit dans ce dépôt : leurs workflows se chevauchent. Une curiosité se fait sur un clone jetable, pas ici. Ne pas remplacer cette constitution par le `AGENTS.md` du dépôt Superpowers.

Mise à jour du submodule : seulement quand un commit est choisi. Pas d’upgrade automatique dans un correctif.

## Définition de terminé

Un changement est terminé seulement si :

- `pytest` est vert, `ruff check .` est vert et `mypy app` est vert (zéro erreur). La CI (`.github/workflows/ci.yml`) bloque la pull request si l’un échoue. Pas de liste d’exceptions Ruff ni de `# type: ignore` sans justification locale.
- Aucun secret n’est loggé.
- Aucun appel réseau (BNF, places de marché, DeepSeek) n’est ajouté avant l’envoi de l’alerte.
- Revue humaine si le diff touche [`app/services/vinted_scraper.py`](app/services/vinted_scraper.py), les formules de prix ou de comparaison marché, ou le chemin ingestion → matching → Telegram.

### Workflow Git

Le dépôt est protégé : le status check `check` est obligatoire et `enforce_admins` est actif, donc le push direct sur `main` est refusé, **y compris par le propriétaire**. Ce n’est pas une précaution de style : la CI ne peut pas jouer le rôle de `check` sur un commit qui n’existe pas encore sur GitHub. La branche est donc la seule voie, et elle est aussi la bonne.

- **Une feature = une branche**, issue de `main`, nommée `feat/`, `fix/`, `docs/` ou `chore/` selon l’intention. Jamais de commit direct sur `main`.
- **Commit et push sur la branche sont libres**, sans demande préalable : c’est le prix de la protection, et la branche supprimée après merge en garde la trace dans l’historique.
- **PR via `gh pr create`, merge en squash**, aligné sur l’historique (`(#12)`, `(#11)`, …).
- **Double vérification** avant merge, les deux passes exigées :
  1. la CI verte — job `check` (et `test-postgres`) au vert sur la PR ;
  2. le second passage local — `make lint && make test` rejoué dans l’arbre de la branche, **après** le push et juste avant le merge.

  `make verify` enchaîne les deux et s’arrête au premier échec. Le merge n’a lieu que si les deux passent : **zéro anomalie** = `pytest`, `ruff` et `mypy` verts tous les trois, sur la branche autant que sur la CI.

```bash
make pr MESSAGE="…" TITLE="…" [BRANCH=fix/isbn] [STAGE="app/… tests/…"]   # branche, commit, push, PR
make verify                                                            # second passage local + attente CI verte
```

Un agent qui implémente une feature travaille donc ainsi : branche depuis `main`, commits, `make pr`, `make verify`, puis merge squash. S’il n’est pas Explicitement demandé de merger, il s’arrête à la PR verte et le dit.

## Produit

BotRadar est un bot Telegram **privé** d’aide à la décision pour l’occasion (BD/comics, vêtements, tech, jeux vidéo).

- Il **détecte** des annonces Vinted et envoie une alerte Telegram instantanée.
- Il **compare** les BD au marché d’occasion livrée (cache Postgres, refresh planifié).
- Il **n’achète pas**, n’ajoute pas au panier, n’envoie pas de message vendeur, n’automatise aucune action marketplace.

Utilisateur unique / allowlist (`TELEGRAM_ALLOWED_USER_IDS`). Domaine public : `https://radar.aminezegmou.fr`.

### Clavier d'alerte Telegram
Chaque alerte Telegram envoyée comporte un clavier inline compact et ergonomique :
- Ligne 1 : `[ 🔗 Annonce ]` (Lien direct Vinted).
- Ligne 2 (BD uniquement) : `[ 📊 Prix du marché ]` (Détails et cotes complètes de la série).
- Ligne 3 (Ligne 2 si hors-BD) : `[ 👎 Faux positif ]` (Enregistrement du dislike pour l'auto-tuning) et `[ 💬 Négocier ]` (Coach IA).
*(Note : Les anciens boutons factices « Payer » et « Dressing / Lot » ont été définitivement supprimés — voir ADR-007 et ADR-011).*

## Stack

- Python 3.12, FastAPI, aiogram 3, SQLAlchemy asyncio, Alembic, Redis, APScheduler, httpx, Pydantic v2.
- Un seul processus de production : `uvicorn app.main:app` dans `radar_app` (API + webhook Telegram + scheduler + poller Vinted).
- PostgreSQL 16, Redis 7, passerelle centralisée `caddy_gateway` via réseau Docker `web_gateway` (HTTPS). Compose : [compose.yml](compose.yml).
- Tests : pytest + pytest-asyncio (531 tests dont 3 benchmarks). Lint : Ruff (ligne 100). Typage : mypy `strict`, zéro erreur — tout nouveau code doit rester propre, pas de régression tolérée.

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

Ne pas inventer une nouvelle couche. Ne pas déplacer `tome_extractor.py`. Les enums (`AlertStatus`, `CategorySlug`, `ComparisonStatus`, `ItemCondition`, `MatchConfidence`) sont définis une seule fois dans `app/domain/enums.py` et réexportés par `app/core/constants.py`.

## Invariant n°1 — l’alerte Telegram d’abord

Toute modification du chemin ingestion → matching → Telegram doit respecter cet ordre :

1. Dédoublonner l’annonce (`source_name` + `external_id`).
2. Matcher en CPU local (`evaluate_listing_against_alert`). Une requête groupée de l'état de notification par annonce (`match_notification_state`), pas un `SELECT` par alerte.
3. Cote BD : **cache local uniquement**. `MarketPriceService.get_comparison_summary` est synchrone, zéro HTTP. Référence unique : le prix le plus bas constaté sur 1 an, sinon le prix du marché du jour (ADR-016). Un livre identifié mais sans cote affiche `PENDING_COTE_LINE` (« cote en cours ») : le silence faisait croire à une absence de prix.
4. `send_listing_alert` — priorité absolue.
5. Ensuite seulement, en `safe_create_task` :
   - enrichissement DeepSeek dans `telegram_service.py` avec le **vrai** `message_id` ;
   - liaison ISBN BNF (`_link_book_from_bnf`) dans une **autre** session ;
   - cotation du livre sans cote utilisable (`schedule_book_pricing`), une fois **toutes** les alertes parties. Le livre vient alors d'être identifié : son titre et son ISBN partent ensemble chez tous les fournisseurs, repli par titre compris, sinon la cote resterait vide et l’alerte suivante serait à nouveau muette.

Interdit sur ce chemin :

- `await` vers BNF, eBay, Momox, Gibert, Recyclivre, Chasse-aux-livres ou le moteur IA **avant** l’envoi.
- Relancer `AsyncEnrichListingUseCase.schedule_enrichment` : cette méthode a été supprimée
  (elle partait avec `message_id=0` et perdait l’enrichissement). Ne pas la réintroduire ;
  l’enrichissement passe par `telegram_service.py` avec le vrai `message_id`.
- Un second client HTTP vers le moteur IA « pour voir ». Un seul POST, dans `BaseLLMClient.complete`.
- `time.sleep` dans `app/`.
- Calculs lourds ou regex globaux « optimisés » sans benchmark reproductible (`make bench` / `tests/performance/`).

Conséquence assumée : la **première** alerte d’une BD inconnue peut partir sans ligne de cote. L’ISBN BNF sert aux suivantes.

## Invariant n°2 — un seul scheduler

- `app/main.py` démarre **toujours** APScheduler. Le poller Vinted est optionnel (`VINTED_POLLER_ENABLED`).
- Jobs : `max_instances=1`, `coalesce=True` (07:00, 19:00 Europe/Paris + cleanup 03:30).
- Compose n’a pas de service worker et il n’existe pas de module `app.worker`. Tout tourne dans `radar_app`.
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
9. **Historique vert** : chaque commit passe `ruff` et `pytest` sur son propre arbre. Un refactor ne dépend jamais d’un commit futur ; un découpage qui produit un commit rouge est un mauvais découpage. `make check-history` rejoue la série (base = `$CHECK_HISTORY_BASE`, sinon dernier tag `v*`, sinon `HEAD~5`) et la CI rejoue la même chose à chaque tag. `mypy` n’est pas dans le lot par défaut : c’est un objectif de bout de branche, pas un contrat de commit.
10. Toute cote de marché passe par les règles pures de [`app/domain/rules/market_offer.py`](app/domain/rules/market_offer.py) : occasion, état au moins « Très bon », marchand non exclu. Ne pas réintroduire un filtre d'état local à un fournisseur ou dans un handler.
11. Toute écriture de cote passe par [`app/services/book_market_pricing.py`](app/services/book_market_pricing.py) : `price_book_now` pour un écran qui attend le résultat, `schedule_book_pricing` pour l'arrière-plan. Ni le handler `/add`, ni le bouton « 📊 Prix du marché », ni l'édition d'un ISBN, ni le job planifié n'ont leur propre collecte d'offres — une copie, c'est une cote qui se contredit selon le chemin qui l'a produite.

## Tests

- Ne supprimer aucun test existant. Adapter si le symbole testé disparaît volontairement.
- Le matching, l’idempotence d’ingestion, le calcul de total, le cache marché et le webhook secret sont des filets critiques.
- Les tests d’ingest patchent l’envoi via un réexport / `sys.modules` : préserver `_get_send_listing_alert()` tant que ce contrat tient.
- Pas de base globale partagée non isolée à introduire dans `conftest.py`.

## Fichiers sensibles — ne pas étendre, ne pas « améliorer » le comportement

Sans demande **explicite** de l’utilisateur :

- [`app/services/vinted_scraper.py`](app/services/vinted_scraper.py) — cookies Redis, Cloudflare, night mode. Le parsing HTML/JSON vit dans [`app/services/vinted_parsing.py`](app/services/vinted_parsing.py) ; ne pas y remettre de réseau.
- [`scripts/vinted_autobuy.user.js`](scripts/vinted_autobuy.user.js)
- [`scripts/sync_vinted_session.py`](scripts/sync_vinted_session.py)
- Formules de `price_calculator.py` / comparaison marché (une offre sans port ≠ total « article seul »).
- [`app/providers/chasse_aux_livres.py`](app/providers/chasse_aux_livres.py) — cookies et anti-bot. Son filtrage d'occasion et sa validation de titre s'appuient désormais sur [`app/domain/rules/market_offer.py`](app/domain/rules/market_offer.py) et `title_similarity` (ADR-016) : ne pas y réintroduire de dictionnaire d'œuvres figé.

Les scripts ne sont importés par `app/` qu’à dessein. `python-dotenv` sert à `scripts/local_vinted_poller.py`, pas au runtime Docker.

## Déploiement production

Hôte : `ubuntu@88.96.56.44`. Clé : `~/.ssh/ssh-key-2026-09-10.key`. Répertoire : `~/bot-radar/`.

Exécuter **soi-même** les commandes (réseau + clé SSH), ne pas demander à l’utilisateur de copier-coller.

Un push ne déploie rien : `~/bot-radar` n’est pas un dépôt git, c’est une cible `rsync` (`--exclude .git`). Les deux dérives qui ont déjà survécu à des pushes verts — code jamais envoyé, `APP_ENV` resté à `development` — sont invisibles depuis le dépôt local.

**La procédure complète est une commande :**

```bash
make deploy           # pré-vol → synchronisation → rebuild → migration → vérification
```

Elle s’arrête au premier échec et n’essaie pas l’étape suivante : la première qui échoue n’est jamais rattrapée par les suivantes. La migration n’est jouée qu’une fois le conteneur sain — migrer pendant le redémarrage viserait l’image précédente et laisserait le schéma en retard d’une version sur le code, sans que rien ne le signale. `make deploy DEPLOY_ARGS=--dry-run` relit l’ordre sans rien exécuter. Les étapes restent accessibles séparément (`make preflight`, `make verify-deploy`) pour reprendre un déploiement interrompu.

Le détail de chaque étape, quand on les joue à la main :

```bash
make preflight        # APP_ENV, secrets, un seul scheduler, migration en tête, écart de code
```

Synchronisation (n’exclut pas `.env` distant : **ne pas** envoyer un `.env` local par accident ; le rsync ci-dessous ne cible pas `.env` s’il est absent du filtre, mais un `.env` local **écraserait** la prod s’il est présent — vérifier avant) :

```bash
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.mypy_cache' --exclude '.ruff_cache' --exclude '.git' --exclude '.env' --exclude 'vendor' \
  -e "ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no" \
  ./ ubuntu@88.96.56.44:~/bot-radar/
```

Rebuild de l’app (obligatoire pour `alembic/` : non monté en volume ; `./app` est bind-mounté) :

```bash
ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 \
  "cd ~/bot-radar && docker compose up -d --build app"
```

Migration **après** que le nouveau conteneur soit healthy :

```bash
ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 \
  "cd ~/bot-radar && docker compose exec -T app alembic upgrade head && docker compose exec -T app alembic current"
```

Contrôles : `radar_app` healthy, `https://radar.aminezegmou.fr/health` 200, pas de conteneur worker, pas de log `Secrets de production vides`, scheduler démarré. Ne pas dumper de tokens dans les logs collectés.

Puis `make verify-deploy` : les mêmes contrôles, mais cette fois un écart de code est fatal. Un déploiement n’est terminé que lorsqu’il passe.

Ne pas committer ni pousser sauf demande. Ne pas `--force` git.

**Protection de `main`** (actrice, à ne pas défaire) : le status check `check` est obligatoire et `enforce_admins` est actif, donc **le force-push est refusé, y compris par le propriétaire**. Elle rend le push direct impossible, et pas par prudence : la CI ne peut pas jouer le role `check` sur un commit qui n’existe pas encore sur GitHub. Le chemin normal n’est donc plus de la lever : c’est la branche et la PR (voir « Workflow Git »). `scripts/push_protected.py` a été supprimé avec cette voie — la constitution ne tolère pas un chemin contourné maintenu en vie à côté du chemin réel.

La procédure de restauration ci-dessous reste la référence, pour le cas où la configuration de la branche doit être rétablie à la main. Lever :

```bash
gh api -X DELETE repos/ZeAmine/bot-radar/branches/main/protection
```

La remettre ensuite — ce corps reproduit l’état mesuré sur l’API, pas l’intention :

```bash
gh api -X PUT repos/ZeAmine/bot-radar/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": { "strict": false, "contexts": ["check"] },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": false,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON
```

Ce que l’API accepte, vérifié par aller-retour `PUT` puis `GET` :

- `required_pull_request_reviews: null` et `restrictions: null` sont acceptés et disparaissent de la réponse : aucune revue exigée, aucune restriction.
- `allow_fork_syncing` **était documenté à `true` alors que GitHub le laisse à `false`**. Le `PUT` ne signale pas l’écart — il répond `200` — et seul le `GET` ultérieur révèle que la valeur n’a pas pris. Le corps ci-dessus porte donc `false`, la valeur réellement appliquée.
- `block_creations` et `required_signatures` apparaissent dans le `GET` mais ne se règlent pas par ce `PUT` : ne pas les ajouter au corps, ils seraient ignorés de la même façon.

Si le job du workflow `check` est renommé, mettre à jour `contexts` dans la même foulée : sinon `main` se verrouille.

Révision Alembic actuelle attendue : `0006_composite_indexes` (index composites alertes/cache). Toute nouvelle migration : fichier dans `alembic/versions/`, rebuild, puis `upgrade head`. **Un identifiant de révision tient dans 32 caractères** : Alembic crée `alembic_version.version_num` en `VARCHAR(32)`, et un identifiant plus long n'échoue qu'à l'écriture de la version, après le DDL de la migration — donc seulement en production. `tests/unit/test_alembic_revisions.py` le vérifie. Pas d’`op.create_index` `CONCURRENTLY` dans une transaction Alembic standard ; table petite uniquement, sinon le demander.

## IA

**Invariant n°4 — le fournisseur IA est un adaptateur, jamais une dépendance (ADR-018).**

`app/providers/llm/` est la **seule** frontière qui connaît un fournisseur. Tout le reste du projet parle au contrat `LLMClient` :

- `base.py` : le contrat (`vendor`, `model`, `is_configured`, `complete`, `generate_json`, `check_health`) et `BaseLLMClient`, qui porte la sémantique commune — activation, client HTTP partagé, absorption des erreurs, repli `None`, sonde de santé en cache 30 s. Un adaptateur écrit cinq méthodes de traduction, pas une seconde implémentation du client.
- `openai_compatible.py` : DeepSeek, Groq, Mistral, OpenRouter, Ollama, LM Studio. `anthropic.py` : API Messages, dont le dialecte ne partage rien avec OpenAI.
- `registry.py` : `AI_PROVIDER` choisit l’adaptateur. Un fournisseur inconnu lève au démarrage : une faute de frappe doit se voir, pas éteindre l’IA en silence.
- Variables neutres : `AI_PROVIDER`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`, `AI_TIMEOUT_SECONDS`. Les noms historiques `DEEPSEEK_*` ont disparu, sans shim de compatibilité.
- `tests/unit/test_llm_contract.py` rejoue le même contrat sur **chaque** adaptateur enregistré. Un nouvel adaptateur y passe sans modification : ne pas affaiblir ce test pour faire passer un adaptateur.

Interdits :

- Nommer un fournisseur (ou `AI_MODEL`) hors de `app/providers/llm/` et de la configuration : ni dans une signature, ni dans un texte utilisateur, ni dans un log, ni dans un nom de test.
- Écrire un `httpx` dans un module IA : tout passe par `BaseLLMClient.complete`.
- Dupliquer une logique d’erreur ou de santé dans un adaptateur : elle est dans la classe de base.
- La clé de cache d’analyse doit inclure fournisseur, modèle et version de prompt (`ListingAnalysisProvider.compute_cache_key`), sinon un changement de modèle continuerait de servir l’ancien résultat en silence pendant 7 jours.
- **Avaler l’erreur ne dispense pas de la montrer** (ADR-005). `BaseLLMClient` repli sur `None`, jamais d’exception : mais une liste vide obtenue parce que personne n’a répondu ne doit jamais se lire dans l’interface comme un avis du moteur. Un module qui rend un avis porte le fait « le moteur a-t-il répondu ? » — `KeywordVariantsOutcome.answered` — et le handler affiche la raison. L’alerte se crée dans les deux cas ; seule la ligne qui l’explique change.

Chemin **vivant** post-alerte : `app/services/ai/` (`AIPostAlertPipeline.enrich_alert`) alimenté par `llm_client`. `data = None` avant l’appel si le client n’est pas configuré.

Chemin **tests / provider Pydantic + cache Redis** : `ListingAnalysisProvider` + `AsyncEnrichListingUseCase.execute`. Ne pas le recâbler sur l’ingestion. L’IA conseille, elle ne bloque jamais l’alerte.

Modules actifs (`app/services/ai/modules/`) :
- `natural_add.py` (`AI_FEATURE_NATURAL_ADD=true`) : Création d'alertes en langage naturel (`/add`).
- `profit_scoring.py` (`AI_FEATURE_PROFIT_SCORING=true`) : Estimation de rentabilité / indice pépite (tech, gaming, vêtements).
- `negotiation.py` (`AI_FEATURE_NEGOTIATION=true`) : Coach de négociation et message d'offre vendeur (`💬 Négocier`).
- `keyword_variants.py` (`AI_FEATURE_KEYWORD_VARIANTS=true`) : Suggestions de variantes orthographiques et d’exclusions, ancrées sur la fiche de l’ouvrage. Sur `/add`, la recherche part en arrière-plan (`safe_create_task`) après le jalon « ouvrage trouvé » : le récap n’attend jamais le moteur, et le résultat est fusionné dans le brouillon seulement s’il porte encore sur l’ouvrage affiché.
- `auto_tuning.py` (`AI_FEATURE_AUTO_TUNING=true`) : Analyse des dislikes et suggestions d'exclusions (`/auto_tune`).
*(Note : Les modules non pertinents `hidden_condition`, `vision_guard`, `bundle_decomposition`, `price_glitch`, ainsi que `bundle_detector` et le bouton dressing ont été définitivement supprimés — voir ADR-006, ADR-007, ADR-011).*

Flags `AI_FEATURE_*` : un flag = un module vivant dans `app/services/ai/modules/` (strictement les 5 ci-dessus). Ne pas réintroduire un flag sans module.

**Autorité ISBN (ADR-014)** : `app/services/book_metadata_service.py` est la seule autorité ISBN du projet. Un ISBN venant du modèle, d'une saisie utilisateur ou d'une URL n'est persisté qu'après confirmation d'une notice BNF dont le titre correspond à l'œuvre demandée (`verify_isbn`). OpenLibrary n'est qu'un repli (`MEDIUM`, jamais en override d'une contradiction BNF) et sa provenance est affichée dans le récap. Les **éditions concurrentes** d'une même œuvre (`BookResolution.editions()`) sont listées dans le récap avec un clavier de bascule (`al_create_edition:*`) : l'ISBN conditionne le matching, l'URL de recherche Vinted et la cote de marché, donc l'utilisateur le choisit avant la création (ADR-017). Ce clavier n'apparaît qu'à partir de deux éditions exploitables : sous ce seuil, il n'y a rien à choisir. Le récap a un titre unique (`RECAP_TITLE`), jamais dépendant du chemin qui y mène. Un ISBN faux n'est pas anodin : il contraint le matching, l'URL Vinted et la cote marché — et **un titre n'est pas une identité** : plusieurs notices de même titre sous des auteurs différents rendent l'œuvre ambiguë, et l'alerte part alors par titre sans rattacher de fiche (ADR-021). Changer d'ISBN change d'ouvrage : toute la fiche d'identité et les mots-clés dérivés sont réécrits, jamais conservés. Une œuvre numérotée est une **série**, pas un tome : quand la saisie ne demande aucun numéro et qu'une notice rendue par l'autorité en porte un, l'alerte est créée sans ISBN, avec le titre de la série et un récap qui le dit (`app/domain/rules/series_detection.py`, alimenté par `BookResolution.series_signal` — ADR-025). La preuve peut venir d'une notice **voisine**, que la résolution ne retient ni comme édition ni comme homonyme ; c'est `BookResolution.notice_titles` qui la porte, et une résolution ambiguë ne prouve rien. Un tome explicitement demandé — y compris par un ISBN saisi — reste un tome : la détection s'efface. Aucun prompt ne doit proposer de liste de faux positifs toute faite : les mots-clés se raisonnent sur la fiche réelle de l'ouvrage (`BookContext`). Une exclusion ne se propose que si elle nomme une confusion réelle : elle n'est jamais le titre plus un numéro de tome, et une liste vide est une réponse valide (`app/domain/rules/keyword_hygiene.py`, ADR-029).

## Architecture déjà tranchée

- Les enums vivent dans `app/domain/enums.py` et sont réexportés par `app/core/constants.py`. Pas une seconde définition.
- Pas d’index `(source_name, received_at)` : le doublon d’annonce est `(source_name, external_id)`, les stats filtrent `received_at` seul.
- Un seul POST moteur IA : `BaseLLMClient.complete`, une fois implémenté. `ListingAnalysisProvider` ajoute la validation Pydantic et le cache Redis pour les tests. Il ne revient pas sur l’ingestion.
- Les modules IA reçoivent leur client par `BaseAIModule(client=...)`, jamais par import du singleton dans un handler.
- Les règles de matching vivent dans `app/domain/rules/listing_match.py`. `matcher_service` ne fait que réexporter. `DeterministicListingMatcher` appelle cette fonction.
- Le JSON des cinq modules IA est validé par un modèle Pydantic à champs optionnels. Un champ absent prend le défaut.
- Le SQL des handlers est dans les repositories (`stats_repo`, `alert_repo`, `book_repo`, `autotune_repo`).
- Le FSM Telegram et le rate limiter utilisent le client Redis de `app.core.redis`.
