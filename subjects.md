# BotRadar — Cartographie des sujets (`subjects.md`)

Ce document cartographie l'ensemble des domaines techniques et fonctionnels de **BotRadar**.
Il sert de boussole aux modèles de langage (LLMs) et assistants de code pour situer immédiatement le rôle de chaque composant, les fichiers concernés, les règles spécifiques et le sens de circulation des données.

### Documentation de référence (Univers LLM)
- [`AGENTS.md`](AGENTS.md) : **Constitution & Règles opérationnelles** (Invariants, architecture, déploiement, qualité).
- [`subjects.md`](subjects.md) : **Cartographie des 12 sujets** (Fichiers clés, rôle, flux de données par composant).
- [`memory.md`](memory.md) : **Mémoire du projet & ADRs** (Historique des arbitrages, ADR-001 à ADR-013, dette différée).

---

## Sommaire des sujets

1. [Sujet 1 — Ingestion & Polling Vinted](#sujet-1--ingestion--polling-vinted)
2. [Sujet 2 — Normalisation & Matching Déterministe](#sujet-2--normalisation--matching-déterministe)
3. [Sujet 3 — Cote BD, BNF & Comparateur de Marché](#sujet-3--cote-bd-bnf--comparateur-de-marché)
4. [Sujet 4 — Bot Telegram (Commandes, FSM, Claviers, Callbacks)](#sujet-4--bot-telegram-commandes-fsm-claviers-callbacks)
5. [Sujet 5 — Auto-Tuning & Apprentissage Proactif (Dislikes)](#sujet-5--auto-tuning--apprentissage-proactif-dislikes)
6. [Sujet 6 — Variantes Orthographiques & Création Assistée](#sujet-6--variantes-orthographiques--création-assistée)
7. [Sujet 7 — Sous-système IA Post-Alerte](#sujet-7--sous-système-ia-post-alerte)
8. [Sujet 8 — Base de Données, Modèles ORM & Migrations Alembic](#sujet-8--base-de-données-modèles-orm--migrations-alembic)
9. [Sujet 9 — Scheduler APScheduler & Tâches d'Arrière-Plan](#sujet-9--scheduler-apscheduler--tâches-darrière-plan)
10. [Sujet 10 — Sécurité, Authentification & Gestion des Secrets](#sujet-10--sécurité-authentification--gestion-des-secrets)
11. [Sujet 11 — Déploiement VPS & Infrastructure Docker Compose](#sujet-11--déploiement-vps--infrastructure-docker-compose)
12. [Sujet 12 — Scripts & Outillage Utilisateur](#sujet-12--scripts--outillage-utilisateur)

---

## Sujet 1 — Ingestion & Polling Vinted

### Rôle
Détecter les nouvelles annonces Vinted en temps réel et les injecter dans le pipeline interne sans bloquer le système.

### Fichiers clés
- [`app/tasks/vinted_poller.py`](app/tasks/vinted_poller.py) : Boucle de polling haute fréquence à cadence adaptative (12s en pointe, 15s standard sur VPS, 8s avec proxy résidentiel), dédoublonnage en mémoire (`seen_ids`), ordonnancement entrelacé (staggered interleaving) des catégories.
- [`app/services/vinted_scraper.py`](app/services/vinted_scraper.py) : Gestionnaire de session Vinted (`VintedSessionManager`), cookies persistés dans Redis, interrogation prioritaire de l'API interne JSON (`svc-catalogue/items`, 38 Ko) avec repli transparent sur le catalogue HTML.
- [`app/services/vinted_parsing.py`](app/services/vinted_parsing.py) : Parsing pur HTML/JSON (`parse_vinted_catalog_json`, `parse_vinted_catalog_html`) et extraction de la photo de couverture. Aucun réseau.
- [`app/api/routes/ingest.py`](app/api/routes/ingest.py) : Endpoint interne sécurisé `POST /api/v1/ingest/listings`.
- [`app/use_cases/ingest_listing.py`](app/use_cases/ingest_listing.py) : Orchestration du flux : dédoublonnage BDD, persistance du listing, déclenchement du matching et alerte Telegram.
- [`app/db/repositories/listing_repo.py`](app/db/repositories/listing_repo.py) : Dédoublonnage par `(source_name, external_id)` et contrainte unique `listing_matches (alert_id, listing_id)`.

### Invariants et contraintes
- **Zéro blocage Cloudflare & API First** : Le poller interroge le microservice interne `/svc-catalogue/items` avec les en-têtes web actuels (`Platform: web`, `X-Next-App: marketplace-web`), et bascule sur le scraping HTML si l'API interne fait défaut.
- **Image de couverture** : La photo de couverture est extraite dès le catalogue et affichée directement dans l'alerte Telegram, sans requête Vinted supplémentaire.
- **Frais acheteur** : Calcul automatique des frais de protection (`calculate_buyer_protection_cents` : 0.70 € + 5%) et frais de port forfaitaires (2.88 €).
- `to_ingest_payload` génère une description synthétique pour le matching car la description vendeur détaillée n'est pas présente sur le flux catalogue.

---

## Sujet 2 — Normalisation & Matching Déterministe

### Rôle
Comparer en quelques microsecondes en mémoire CPU chaque annonce entrante avec l'ensemble des alertes actives de l'utilisateur.

### Fichiers clés
- [`app/services/matcher_service.py`](app/services/matcher_service.py) : Évaluation multicritère :
  - `evaluate_listing_against_alert(...)` : vérification du prix max, catégorie, inclusion de mots-clés, variantes, exclusions et états.
  - `ListingRepository.matched_alert_ids(...)` : matches déjà notifiés pour une annonce, une seule requête SQL. Le matching lui-même est CPU (`evaluate_listing_against_alert`).
- [`app/domain/rules/matcher.py`](app/domain/rules/matcher.py) : Spécifications métier pures du matching (`DeterministicListingMatcher`).
- [`app/utils/text.py`](app/utils/text.py) : Normalisation Unicode avancée : passage en minuscules, décomposition NFKD (suppression des accents), remplacement des apostrophes typographiques et séparateurs.

### Invariants et contraintes
- **CPU local strict** : Aucun appel HTTP, aucun modèle d'embedding ou vector search externe lors du matching.
- **Requête groupée** : Ne jamais exécuter un `SELECT` individuel par alerte. Les alertes actives sont chargées en bloc.
- **Exclusion prioritaire** : Si un terme exclu (`exclude_keywords`) est trouvé dans le titre ou les mots-clés, le match est immédiatement rejeté (`is_match = False`).

---

## Sujet 3 — Cote BD, BNF & Comparateur de Marché

### Rôle
Calculer instantanément la cote et l'écart de prix pour les bandes dessinées et comics par rapport au marché de l'occasion livrée (eBay, Momox, Gibert, Recyclivre, Chasse-aux-livres).

### Fichiers clés
- [`app/services/market_price_service.py`](app/services/market_price_service.py) : `get_comparison_summary(...)` et `format_market_details_card(...)` synchrones, lisant exclusivement le cache local PostgreSQL. `reference_price(cache)` fixe la règle de référence ; `has_usable_price(cache)` dit si une cote permet de comparer, `format_cote_lines(book)` et `PENDING_COTE_LINE` portent les lignes d'attente ; `_vinted_total_cents`, `_select_secondhand_partners` et `_series_books_lines` sont les helpers partagés (ADR-016). La référence est le plus bas prix que nous puissions constater, borné par nos relevés (ADR-019).
- [`app/services/market_health.py`](app/services/market_health.py) : `notify_stale_certified_lows` signale, une fois par jour, les cotes affichées que plus aucun relevé ne nourrit. Appelée en fin de passe par `refresh_book_prices_task`, jamais fatale (ADR-020).
- [`app/services/book_market_pricing.py`](app/services/book_market_pricing.py) : **unique point de cotation**. `fetch_offers_for_book` interroge tous les fournisseurs avec l'ISBN validé **et** le titre ensemble (le repli par titre est ce qui permet de coter un ouvrage introuvable par code) ; `price_book_now` attend le résultat pour un écran, `schedule_book_pricing` part en arrière-plan avec une garde d'un seul vol par livre. Consumé par la création d'alerte, l'édition d'un ISBN, le bouton « 📊 Prix du marché » et le job planifié.
- [`app/domain/rules/market_offer.py`](app/domain/rules/market_offer.py) : règles pures de classification d'une offre (`condition_tier`, `is_merchant_excluded`, `is_comparable_secondhand_offer`). Source unique du filtre d'occasion, partagée par le registre de fournisseurs, Chasse-aux-livres et l'affichage (ADR-016).
- [`app/services/tome_extractor.py`](app/services/tome_extractor.py) : Extraction déterministe des numéros de tomes, intégrales et lots (regex ultra-rapides sans I/O). Un lot dénombré sans numéros posés (« lot de 4 tomes ») remonte `lot_count` ; c'est `resolve_loaded_books` qui décide s'il peut le composer.
- [`app/services/book_catalog_service.py`](app/services/book_catalog_service.py) : Association d'une alerte à une série ou un livre précis.
- [`app/services/book_metadata_service.py`](app/services/book_metadata_service.py) : **Autorité ISBN** du projet — BNF en source primaire, OpenLibrary en repli (`_openlibrary_works`, confiance `MEDIUM`, jamais en override). `resolve_for_alert(titre, auteur)` (notice de référence + éditions alternatives), `verify_isbn(isbn, titre)` (confirme le couple ISBN ↔ œuvre), `title_similarity` / `same_author` (ADR-014), `BookResolution.editions()` (éditions concurrentes, ADR-017), `format_edition_fragments` (condensation d'une notice BNF en « 224 p. · 24 cm »).
- [`app/providers/`](app/providers/) : Clients HTTP pour les sources de marché :
  - `ebay_browse.py` : eBay Browse API OAuth2 (recherche par ISBN / titre d'occasion livrée en France).
  - BNF : `book_metadata_service.py`.
  - `momox.py`, `chasse_aux_livres.py`, etc.
- [`app/db/repositories/market_price_repo.py`](app/db/repositories/market_price_repo.py) : Cache de prix (`book_market_cache`), persistance des snapshots (`market_price_snapshots`) et cotes.

### Invariants et contraintes
- **Choix de l'œuvre, puis de l'édition, avant création** : le récap `/add` décrit l'édition surveillée (« 📖 Édition suivie ») et le clavier `al_create_edition:*` permet de changer d'ISBN sans refaire la commande ; les boutons sont numérotés `1️⃣ Éditeur - année` comme le récap. Ni la ligne ni les boutons n'apparaissent sous **deux éditions exploitables** : sans alternative, la ligne « 🏢 Éditeur » suffit. Le rendu passe par `recap_from_state(draft)` et `creation_keyboard(draft)` : un seul chemin, éditions comprises.
- **Œuvres homonymes (ADR-021)** : quand plusieurs notices portent le titre demandé sous des auteurs différents, rien n'est rattaché — ni ISBN, ni auteur, ni éditeur — et le clavier propose un bouton par œuvre (`al_create_work:*`, « 1️⃣ Auteur · année · éditeur ») **avant** les éditions. Choisir l'œuvre réécrit la fiche d'identité et relance la recherche de mots-clés, comme le fait toute correction d'ISBN (`realign_draft_to_isbn`).
- **Zéro HTTP sur le chemin de l'alerte** : `MarketPriceService.get_comparison_summary` lit uniquement la table locale `book_market_cache`. Le refresh complet interroge les providers à 07:00 et 19:00 ; un lot incrémental tourne à `:15` et `:45`.
- **Référence de prix (ADR-016)** : le **prix le plus bas constaté sur 1 an** prime toujours ; le prix du marché du jour n'est qu'un repli. L'écart compare le **total Vinted, frais de port inclus**, à cette référence. Le résumé d'alerte et le panneau détaillé citent la même référence.
- **Format d'affichage** :
  ```text
  📉 Prix bas constaté : 12,50 €
  ✅ Écart : -3,50 €
  ```
- **Zéro faux positif de cotation** : une offre neuve ou en état déclaré inférieur à « Très bon » n'entre jamais dans une cote ; un ISBN invalide n'est jamais utilisé pour interroger un fournisseur ; un lot n'est cumulé que sur les tomes appariés, la couverture étant annoncée (ex: `Lot, 2/3 tomes`).
- **Lot dénombré** : quand le vendeur donne un compte et aucun numéro (« lot de 4 tomes »), `_resolve_counted_lot` ne compose le lot que si la série compte exactement ce nombre de tomes identifiables, sans doublon d'édition. La composition est alors marquée `composition_inferred` et l'alerte l'affiche comme telle (`Lot de 4 tomes, composition déduite`) : une déduction ne doit jamais se lire comme un inventaire énoncé par le vendeur.
- **Tome concerné en gras** : Dans les listes d'autres tomes d'une série, le tome ciblé est mis en évidence en gras. Les tomes sans cote disponible sont masqués.

---

## Sujet 4 — Bot Telegram (Commandes, FSM, Claviers, Callbacks)

### Rôle
Interface utilisateur conversationnelle pour piloter les alertes, recevoir les notifications, signaler les faux positifs et négocier.

### Fichiers clés
- [`app/bot/bot_instance.py`](app/bot/bot_instance.py) : Initialisation du `Bot` aiogram 3 et du `Dispatcher`.
- [`app/bot/middlewares/auth.py`](app/bot/middlewares/auth.py) : Contrôle d'accès strict par allowlist (`TELEGRAM_ALLOWED_USER_IDS`).
- [`app/bot/handlers/alerts_create.py`](app/bot/handlers/alerts_create.py) : Flow conversationnel FSM `/add` jusqu'au récapitulatif (titre, catégorie, prix max, suggestion IA).
- [`app/bot/handlers/alerts_create_confirm.py`](app/bot/handlers/alerts_create_confirm.py) : Confirmation, annulation et édition du même flux, sur le routeur partagé `_alerts_router`.
- [`app/bot/handlers/alerts_manage.py`](app/bot/handlers/alerts_manage.py) : Commandes `/show`, `/edit`, `/pause`, `/resume`, `/delete`.
- [`app/bot/handlers/callbacks.py`](app/bot/handlers/callbacks.py) : Gestion des boutons inline :
  - `market:{listing_id}` : Vue détaillée des offres du marché d'occasion.
  - `dislike:{listing_id}` : Signalement de faux positif avec confirmation.
  - `negotiate:{listing_id}` : Génération d'une proposition de message de négociation.
- [`app/bot/keyboards/listings.py`](app/bot/keyboards/listings.py) : Clavier Telegram des annonces :
  - Ligne 1 : `[ 🔗 Annonce ]`
  - Ligne 2 : `[ 📊 Prix du marché ]` (uniquement si BD/Comics)
  - Ligne 3 (Ligne 2 si hors-BD) : `[ 👎 Faux positif ]` `[ 💬 Négocier ]`
  *(Note : Les boutons factices « Payer » et « Dressing / Lot » ont été définitivement supprimés).*

---

## Sujet 5 — Auto-Tuning & Apprentissage Proactif (Dislikes)

### Rôle
Permettre au bot d'apprendre des rejets de l'utilisateur : quand un utilisateur clique sur `[👎 Faux positif]`, le moteur IA analyse les motifs parasites récurrents et suggère des mots-clés d'exclusion ciblés pour assainir l'alerte.

### Fichiers clés
- [`app/bot/handlers/autotune.py`](app/bot/handlers/autotune.py) :
  - Commande `/autotune` : hub des alertes ayant des faux positifs non résolus.
  - `process_autotune_for_alert(...)` : exécution de l'analyse avec message de chargement transparent.
- [`app/services/ai/modules/auto_tuning.py`](app/services/ai/modules/auto_tuning.py) : `build_exclusion_proposal(...)` porte **la règle unique** de construction d'une proposition — seuil de faux positifs, appel au moteur, retrait de ce que l'alerte exclut déjà. Le scan planifié et l'analyse déclenchée par l'utilisateur passent tous deux par elle ; ils ne diffèrent que par `min_dislikes`. `claim_autotune_slot` / `release_autotune_claim` encadrent l'émission.
  - `handle_tune_edit(...)` : passage en mode personnalisation manuelle.
  - `handle_custom_exclusions_input(...)` : réception et parsing de la liste modifiée par l'utilisateur.
  - `handle_tune_apply(...)` : application des exclusions et purge des faux positifs résolus.
- [`app/bot/states/alert_creation.py`](app/bot/states/alert_creation.py) : Machine à états `AutoTuneState` avec `waiting_for_custom_exclusions`.
- [`app/services/ai/modules/auto_tuning.py`](app/services/ai/modules/auto_tuning.py) : Module IA promptant le moteur avec l'historique des titres rejetés.

### Invariants et contraintes
- **Créneau d'émission atomique** : une proposition proactive réserve son créneau par `SET … NX` **avant** l'appel au moteur (`claim_autotune_slot`). Le scan planifié et le clic « Faux positif » peuvent donc démarrer la même analyse sans produire deux messages. Sans Redis, la réservation échoue et l'émission est refusée : mieux vaut ne pas proposer que proposer en double. Le créneau est relâché (`release_autotune_claim`) si rien n'a été émis.
- **Format copiable** : Les exclusions suggérées sont envoyées dans un bloc `<code>...</code>` pour faciliter la copie par simple tap sur Telegram.
- **Personnalisation** : L'utilisateur peut copier, modifier (supprimer, ajouter des termes séparés par virgules, points-virgules ou sauts de ligne) et renvoyer sa liste via `[ ✏️ Modifier la liste ]`.
- **Purge à l'application** : Dès que l'utilisateur clique sur `[ ✅ Appliquer les exclusions ]`, les faux positifs (`BotEvent` avec `event_type='listing_disliked'`) rattachés à cette alerte sont supprimés pour ne plus polluer `/autotune`.
- **Conservation à l'annulation** : En cas de clic sur `[ ← Annuler ]`, les dislikes sont scrupuleusement conservés.

---

## Sujet 6 — Variantes Orthographiques & Création Assistée

### Rôle
Aider l'utilisateur lors de la création (`/add`) ou de l'édition (`/edit`) d'une alerte en générant automatiquement les fautes courantes, variantes d'écriture et exclusions standard.

### Fichiers clés
- [`app/services/ai/modules/keyword_variants.py`](app/services/ai/modules/keyword_variants.py) : Module générant variantes orthographiques (ex: *onepiece*, *one-piece*, *one piece*) et faux positifs **ancrés sur la fiche réelle** de l'ouvrage (`BookContext` : auteur, éditeur, année, format, ISBN). Aucune liste de goodies toute faite dans le prompt (ADR-014).
- [`app/bot/handlers/alerts_create.py`](app/bot/handlers/alerts_create.py) : Handler `handle_create_ai_variants` branché sur le bouton `[ ✨ Suggérer variantes IA ]`.
- [`app/bot/handlers/alerts_manage.py`](app/bot/handlers/alerts_manage.py) : variante appliquée à une alerte existante, dans la branche `ai_variants` de `prompt_new_field_value(...)` ; la confirmation est traitée par `handle_var_apply` (`ai_var_apply:`) dans [`app/bot/handlers/autotune.py`](app/bot/handlers/autotune.py).
- **Store des variantes** : `save_pending_variants` / `get_pending_variants` / `pop_pending_variants` dans [`app/bot/handlers/autotune.py`](app/bot/handlers/autotune.py). Volontairement en mémoire seule, contrairement au store d'exclusions (Redis) : ces suggestions sont jetables et se régénèrent d'un `/autotune`.

### Invariants et contraintes
- **Transparence d'exécution** : Un clic sur le bouton affiche immédiatement un indicateur de chargement (`⏳ Recherche des variantes en cours...`) avec verrou anti-double clic pour éviter toute fuite d'appels API ou double facturation de tokens.

---

## Sujet 7 — Sous-système IA Post-Alerte

### Rôle
Enrichir asynchroniquement les alertes Telegram déjà délivrées pour fournir un conseil d'aide à la décision (analyse de lot, estimation de revente, négociation). Le moteur est interchangeable : aucun module ni handler ne connaît le fournisseur.

### Fichiers clés
- [`app/providers/llm/base.py`](app/providers/llm/base.py) : contrat `LLMClient` et `BaseLLMClient`. Seule frontière du projet qui connaît un fournisseur ; porte le client HTTP partagé, l'absorption des erreurs, le repli `None` et la sonde de santé.
- [`app/providers/llm/openai_compatible.py`](app/providers/llm/openai_compatible.py) / [`anthropic.py`](app/providers/llm/anthropic.py) : adaptateurs, cinq méthodes de traduction chacun.
- [`app/providers/llm/registry.py`](app/providers/llm/registry.py) : `AI_PROVIDER` → adaptateur ; `llm_client` est le singleton du projet.
- [`app/providers/llm/listing_analysis.py`](app/providers/llm/listing_analysis.py) : analyse structurée validée par schéma, mémoïsée en Redis (clé incluant fournisseur et modèle).
- [`app/services/ai/pipeline.py`](app/services/ai/pipeline.py) : `AIPostAlertPipeline.enrich_alert(...)` exécuté en arrière-plan (`safe_create_task`) avec le vrai `message_id` Telegram.
- [`app/services/ai/registry.py`](app/services/ai/registry.py) : `AIRegistry` centralisant l'activation des modules post-alerte.
- [`app/services/ai/modules/`](app/services/ai/modules/) :
  - `natural_add.py` : Extraction des critères depuis une phrase en langage naturel (`/add`). L'ISBN proposé n'est qu'un candidat : il n'est retenu que s'il est confirmé par la BNF pour l'œuvre demandée (ADR-014).
  - `profit_scoring.py` : Évaluation du potentiel de revente et marge estimée.
  - `negotiation.py` : Proposition d'offre et message de négociation courtois.
  - `auto_tuning.py` : Suggestions d'exclusions sur faux positifs.
  - `keyword_variants.py` : Suggestions de variantes orthographiques.

### Invariants et contraintes
- **L'IA ne bloque JAMAIS l'alerte** : L'IA intervient uniquement après l'envoi du premier message.
- **Le fournisseur est un adaptateur (ADR-018)** : changer de modèle est une modification du `.env`. Un fournisseur au dialecte inconnu demande un fichier dans `app/providers/llm/` et une ligne dans `LLM_ADAPTERS` — rien d'autre. `tests/unit/test_llm_contract.py` rejoue le contrat sur chaque adaptateur enregistré.
- **Règle un flag = un module** : Chaque flag `AI_FEATURE_*` dans `config.py` correspond strictement à un module actif du dossier.
- *(Note de décision : Les modules superflus `hidden_condition`, `vision_guard`, `bundle_decomposition` et `price_glitch` ont été supprimés — voir ADR-006 et ADR-011).*

---

## Sujet 8 — Base de Données, Modèles ORM & Migrations Alembic

### Rôle
Persister les entités métier, les alertes, les annonces, le catalogue de livres, l'historique des prix et les événements utilisateurs.

### Fichiers clés
- [`app/db/session.py`](app/db/session.py) : Moteur async PostgreSQL (`AsyncSessionLocal`, pool `asyncpg`).
- [`app/db/models/`](app/db/models/) :
  - `alert.py` : Modèle `Alert` (nom, mots-clés, exclusions, variantes, prix max, conditions, statut).
  - `listing.py` : Modèle `Listing` (données de l'annonce, source, prix, URL).
  - `book.py` : Modèle `Book` (catalogue BD : titre, série, tome, ISBN-13 BNF).
  - `book_market_cache.py` : cache de cote (`book_market_cache`). `market_price_snapshot.py` : snapshots (`market_price_snapshots`).
  - `bot_event.py` : Modèle `BotEvent` (suivi des interactions : faux positifs `listing_disliked`, clics).
  - `user.py` : Modèle `User` (identifiant Telegram, configuration).
- [`alembic/versions/`](alembic/versions/) : Fichiers de migration de schéma. Révision courante attendue : `0005_correct_historical_low`.

### Invariants et contraintes
- **Sessions isolées en tâche de fond** : Ne jamais réutiliser la session HTTP/API dans une coroutine d'arrière-plan (`safe_create_task`). Toujours ouvrir un nouveau contexte : `async with AsyncSessionLocal() as session:`.

---

## Sujet 9 — Scheduler APScheduler & Tâches d'Arrière-Plan

### Rôle
Orchestrer le rafraîchissement périodique des cotes du marché et le nettoyage des annonces expirées.

### Fichiers clés
- [`app/tasks/scheduler.py`](app/tasks/scheduler.py) : Configuration APScheduler unifiée.
- [`app/tasks/refresh_book_prices.py`](app/tasks/refresh_book_prices.py) : Job à **07:00** et **19:00**, plus un lot incrémental à **:15** et **:45** (Europe/Paris). Il ne fait qu'ordonner les vagues : la collecte et la consolidation viennent de [`app/services/book_market_pricing.py`](app/services/book_market_pricing.py), le même chemin que la création d'une alerte.
- [`app/tasks/cleanup.py`](app/tasks/cleanup.py) : Job nocturne à **03:30** purgeant les annonces obsolètes et les vieux snapshots.

### Invariants et contraintes
- **Un seul scheduler (Invariant n°2)** : `app/main.py` démarre toujours APScheduler.
- **Un seul processus** : Compose n'a pas de service worker. Le scheduler tourne dans `radar_app`.

---

## Sujet 10 — Sécurité, Authentification & Gestion des Secrets

### Rôle
Garantir la confidentialité absolue du bot privé, l'intégrité des webhooks et le masquage des secrets dans les logs.

### Fichiers clés
- [`app/core/config.py`](app/core/config.py) : Validation stricte des variables d'environnement (`SecretStr`).
- [`app/core/security.py`](app/core/security.py) : Validation sécurisée par `secrets.compare_digest`.
- [`app/core/logging.py`](app/core/logging.py) : `JSONFormatter` masquant automatiquement Bearer, jetons, mots de passe et clés `sk-`.

### Invariants et contraintes
- **Refus de démarrage en production** : Si `APP_ENV=production` et qu'un secret obligatoire (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `INGEST_API_TOKEN`, `INTERNAL_API_TOKEN`) est vide, `validate_production_secrets()` bloque le démarrage.
- **Zéro fuite dans les logs** : Ne jamais afficher de token en clair.

---

## Sujet 11 — Déploiement VPS & Infrastructure Docker Compose

### Rôle
Déployer et maintenir le bot en production sur le serveur Ubuntu dédié.

### Données d'infrastructure
- **Hôte** : `ubuntu@88.96.56.44`
- **Clé SSH** : `~/.ssh/ssh-key-2026-09-10.key`
- **Répertoire** : `~/bot-radar/`
- **Conteneurs Compose** : `radar_app` (FastAPI + bot + poller), `radar_postgres` (Postgres 16), `radar_redis` (Redis 7), raccordés à la passerelle centralisée `caddy_gateway` via le réseau Docker externe partagé `web_gateway`.

### Procédure de synchronisation et redémarrage
```bash
# 1. Synchronisation (sans écraser le .env distant)
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.git' --exclude '.env' --exclude 'vendor' \
  -e "ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no" \
  ./ ubuntu@88.96.56.44:~/bot-radar/

# 2. Redémarrage ou rebuild
ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 \
  "cd ~/bot-radar && docker compose up -d --build app"

# 3. Contrôle de santé
ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 \
  "curl -s https://radar.aminezegmou.fr/health"
```

---

## Sujet 12 — Scripts & Outillage Utilisateur

### Rôle
Fournir des utilitaires pour synchroniser la session Vinted et faciliter le test en local.

### Fichiers clés
- [`scripts/sync_vinted_session.py`](scripts/sync_vinted_session.py) : Script pour pousser manuellement des cookies de session Vinted valides dans Redis (`radar:vinted_session_cookies`).
- [`scripts/sync_chasse_session.py`](scripts/sync_chasse_session.py) : capture et injecte le jeton anti-bot Chasse-aux-livres. Le jeton n'est poussé qu'après validation par le provider lui-même ; injecté par `POST /api/v1/internal/chasse/session` (ADR-020).
- [`scripts/local_vinted_poller.py`](scripts/local_vinted_poller.py) : Poller Vinted autonome pour tester la détection en environnement de développement local.
- [`scripts/audit_alerts_identity.py`](scripts/audit_alerts_identity.py) : Audit **en lecture seule** des alertes porteuses d'un ISBN, confronté à leur notice BNF : conflit d'ISBN, ISBN inconnu, divergence de titre / auteur / éditeur, variante portant le code d'un autre ouvrage (`carries_foreign_isbn`, la définition du matching). Codes de sortie : 0 rien à signaler, 1 anomalies, 2 erreur. Une panne réseau met l'alerte de côté au lieu de la compter en anomalie : le verdict du service, `consult_authorities` (`IsbnVerdict`), distingue une autorité qui a tranché d'une panne, et écrit la liste ordonnée des autorités réellement consultées ; `bnf_joignable` couvre le cas global, où aucune notice n'est demandée et la sortie est 2. Chaque verdict nomme l'autorité réellement consultée, lue sur `BookMetadata.source` : un repli OpenLibrary s'affiche `REPLI` et compte à part, jamais comme un `ok` ni comme un rapport « conforme ». Une notice de repli obtenue après une panne de la BNF n'est pas un repli : l'alerte est mise de côté et comptée « non vérifiée ». Chaque rapport est daté et nomme sa population (effectif audité, inactives comprises) : deux exécutions de la même commande se comparent, et le `--json` est un objet `{genere_le, population, alertes}`.
- [`scripts/vinted_autobuy.user.js`](scripts/vinted_autobuy.user.js) : Userscript navigateur (Tampermonkey/Violentmonkey) pour accélérer le choix du point de livraison sur Vinted lors de l'achat manuel.

### Invariants et contraintes
- **Fichiers sensibles** : Ne pas modifier ni altérer ces scripts sans demande explicite de l'utilisateur.
