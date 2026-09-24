# Radar Bot — Mémoire du projet & Décisions architecturales (`memory.md`)

Ce document consigne la mémoire vivante de **Radar Bot**, la genèse de ses choix d'architecture (ADRs), ses arbitrages passés et la liste explicite de sa dette technique acceptée.

Il est destiné à être lu par les modèles de langage (LLMs) et ingénieurs pour comprendre le **pourquoi** derrière les contraintes strictes définies dans [`AGENTS.md`](file:///Users/aminezegmou/Projets/Radar%20Bot/AGENTS.md) et la cartographie technique de [`subjects.md`](file:///Users/aminezegmou/Projets/Radar%20Bot/subjects.md).

### Documentation de référence (Univers LLM)
- [`AGENTS.md`](file:///Users/aminezegmou/Projets/Radar%20Bot/AGENTS.md) : **Constitution & Règles opérationnelles** (Invariants, architecture, déploiement, qualité).
- [`subjects.md`](file:///Users/aminezegmou/Projets/Radar%20Bot/subjects.md) : **Cartographie des 12 sujets** (Fichiers clés, rôle, flux de données par composant).
- [`memory.md`](file:///Users/aminezegmou/Projets/Radar%20Bot/memory.md) : **Mémoire du projet & ADRs** (Historique des arbitrages, ADR-001 à ADR-010, dette différée).

---

## 1. Thèse & Philosophie Produit

### Le problème
Sur les plateformes d'occasion (notamment Vinted), les excellentes affaires sur les bandes dessinées de collection, la tech ou les vêtements prisés disparaissent souvent en moins de **60 à 90 secondes**.

### La réponse de Radar Bot
Radar Bot est un assistant privé d'aide à la décision conçu pour maximiser la vitesse d'acquisition de l'acheteur ("snipe").

```
[ Ingestion Vinted ]
        ↓ (~100ms)
[ Matching Déterministe CPU ]
        ↓ (~2ms)
[ Cote BD (Cache Postgres Local) ]
        ↓ (~1ms)
[ ⚡ ALERTE TELEGRAM INSTANTANÉE ]  <-- Objectif prioritaire : < 500ms
        ↓ (Arrière-plan / non bloquant)
[ IA DeepSeek / Liaison BNF ]
```

### Ce que Radar Bot EST
- Un détecteur ultra-rapide d'annonces ciblées.
- Un comparateur de prix instantané par rapport au marché réel de l'occasion livrée.
- Un copilote de négociation et d'évaluation de rentabilité (via IA post-alerte).

### Ce que Radar Bot N'EST PAS (et ne sera jamais)
- **Pas de bot d'achat automatique** : aucune automatisation de checkout sur Vinted.
- **Pas de bot d'interaction marketplace** : pas de message automatique envoyé au vendeur, pas d'ajout au panier via API ou scripts non autorisés sur les serveurs Vinted.
- **Pas d'application multi-tenant ouverte au public** : mono-utilisateur avec allowlist stricte (`TELEGRAM_ALLOWED_USER_IDS`).

---

## 2. Architecture Decision Records (ADRs)

### ADR-001 : Monolithe asynchrone à processus unique (`radar_app`)
- **Contexte** : Le bot a besoin d'une API web (webhooks Telegram, ingestion HTTP), d'un scheduler de tâches (actualisation des cotes BD) et d'un poller Vinted périodique.
- **Décision** : Tout s'exécute dans un **processus unique** FastAPI/uvicorn (`radar_app`). APScheduler est démarré au `lifespan` de FastAPI.
- **Raison** :
  - Déploiement sur un VPS modeste (1 vCPU, 2 Go de RAM).
  - Évite la complexité de coordination distribuée (pas besoin de Celery ou de verrous de tâches Redis complexes pour le scheduler).
  - Réduit l'empreinte mémoire à moins de 300 Mo tout compris.
- **Conséquence** : Ne **jamais** démarrer `python -m app.worker` en parallèle de `radar_app`.

### ADR-002 : Invariant n°1 — Alerte Telegram d'abord, I/O réseau différées
- **Contexte** : Lors de la découverte d'une annonce, enrichir l'annonce avec des cotes BNF, eBay ou une analyse DeepSeek prenait 2 à 5 secondes par annonce. Pendant ce temps, l'article pouvait déjà être vendu sur Vinted.
- **Décision** : La notification Telegram est envoyée **immédiatement** après matching local CPU et consultation du cache PostgreSQL local.
- **Raison** : Le temps est le facteur le plus critique.
- **Conséquence** :
  - Tout appel externe (BNF, eBay, DeepSeek) est interdit avant `send_listing_alert`.
  - L'enrichissement IA se fait en tâche de fond (`safe_create_task`) et vient modifier ou commenter le message Telegram avec son vrai `message_id`.
  - Si un livre est inconnu en base, sa première alerte part sans cote : le fetch BNF d'arrière-plan enrichira la base pour les alertes suivantes.

### ADR-003 : Matching déterministe local en mémoire CPU
- **Contexte** : Évaluer la pertinence d'une annonce face à des dizaines ou centaines d'alertes utilisateurs.
- **Décision** : Le matching repose sur une normalisation Unicode NFKD rigoureuse, des expressions régulières compilées et des comparaisons scalaires (prix, état, exclusions) en mémoire vive.
- **Raison** : Temps d'exécution unitaire < 100 microsecondes. Fiabilité totale : un mot exclu rejettera toujours l'annonce, là où un modèle vectoriel ou LLM peut halluciner ou laisser passer des faux positifs.
- **Conséquence** : Pas de vector store (Milvus, Chroma, pgvector) ni de LLM sur le chemin critique du matching.

### ADR-004 : Cache local de cote BD avec rafraîchissement bi-quotidien
- **Contexte** : Comparer le prix d'une BD à eBay, Momox, Gibert, Recyclivre, etc.
- **Décision** : La consultation pendant l'alerte lit **exclusivement** la table `market_prices`. Les fournisseurs externes sont interrogés uniquement à 07:00 et 19:00 par des jobs planifiés.
- **Raison** : Protection contre les rate limits des API marchandes et zéro latence réseau lors de l'alerte.

### ADR-005 : Rôle purement consultatif de l'IA (DeepSeek)
- **Contexte** : L'IA apporte une forte valeur ajoutée (conseils de négociation, analyse du prix par rapport à l'état, détection de rééditions).
- **Décision** : L'IA ne bloque jamais le flux principal. Le pipeline IA post-alerte (`AIPostAlertPipeline`) intervient en arrière-plan. En cas d'erreur API, de timeout ou de quota épuisé, l'alerte initiale est déjà entre les mains de l'utilisateur.

### ADR-006 : Suppression de la détection des défauts cachés ("Hidden Condition")
- **Contexte** : Une feature expérimentale tentait d'analyser la description complète du vendeur pour y déceler des défauts (coins tassés, pages jaunies, manques).
- **Décision** : Supprimée complètement du codebase.
- **Raison** :
  - Le flux standard d'ingestion Vinted utilise le catalogue public où la description détaillée n'est pas disponible.
  - Récupérer la page complète de l'article nécessitait une requête HTTP individuelle supplémentaire (`/items/{id}`), ce qui augmentait considérablement le risque de blocage par le WAF Cloudflare de Vinted.
  - La valeur apportée ne justifiait pas la consommation de tokens et le risque d'éviction du scraper.

### ADR-007 : Suppression du bouton "Payer" (`[ 💳 Payer ]`)
- **Contexte** : L'alerte comportait un bouton inline "Payer" redirigeant vers une URL d'achat Vinted.
- **Décision** : Bouton et callbacks associés (`pay_alert:*`) intégralement supprimés.
- **Raison** :
  - Vinted requiert une authentification dans l'application native ou le navigateur pour finaliser le panier ; un lien de checkout direct expirait fréquemment ou provoquait des erreurs 403.
  - L'utilisateur utilise systématiquement le bouton principal `[ 🔗 Voir l'annonce ]`, qui ouvre l'application Vinted avec ses sessions et moyens de paiement pré-enregistrés.
  - Allègement visuel du clavier Telegram (passage à 3 lignes pour les BD, 2 pour le reste).

### ADR-008 : Auto-Tuning conversationnel & Purge des faux positifs
- **Contexte** : Lorsqu'un utilisateur cliquait sur `[ 👎 Pas intéressé ]`, l'annonce était enregistrée dans `alert_dislikes`. Cependant, après avoir accepté les exclusions recommandées par l'IA via `/auto-tune`, les faux positifs continuaient d'apparaître dans la liste d'auto-tuning. De plus, l'utilisateur ne pouvait pas ajuster les mots-clés suggérés.
- **Décision** :
  1. Introduction d'une machine à états FSM (`AutoTuneState.waiting_for_custom_exclusions`) permettant à l'utilisateur de copier la suggestion, de la modifier manuellement dans Telegram, et de la soumettre.
  2. Purge atomique des entrées `alert_dislikes` pour l'alerte dès confirmation des exclusions (qu'elles soient appliquées telles quelles ou personnalisées).
- **Raison** : Clôture propre de la boucle de feedback utilisateur : un problème corrigé ne doit plus polluer l'interface.

### ADR-009 : Isolation stricte des sessions de base de données en arrière-plan
- **Contexte** : Les tâches asyncio déclenchées via `safe_create_task` (comme la liaison BNF post-alerte ou l'analyse IA) partageaient initialement la session SQLAlchemy du handler de requête principal.
- **Décision** : Toute tâche asynchrone d'arrière-plan doit instancier sa propre session via `async with AsyncSessionLocal() as session:`.
- **Raison** : Évite les conflits de concurrence sur la session, les `PendingRollbackError` et les fermetures prématurées de transactions lors du cycle de vie de la requête parente.

### ADR-010 : Gestion tolérante des séries BD spéciales (ex: 1629, RIP, Blast)
- **Contexte** : Certaines œuvres possèdent des titres courts ("RIP", "Blast"), des chiffres ("1629") ou des formats hors-normes (intégrales sans numéro, diptyques).
- **Décision** :
  - `tome_extractor.py` analyse les motifs "Partie X", "Livre X", "Tome X", "Volume X" sans présupposer une numérotation continue standard.
  - Si un tome spécifique ne peut pas être déduit avec certitude (ex: une annonce globale "BD 1629"), l'alerte est émise sans bloquer, et l'affichage de la cote s'adapte élégamment en indiquant la cote globale de série si disponible ou en omettant la ligne d'écart sans générer d'erreur.

### ADR-011 : Purge des use cases parasites et modules IA inadaptés
- **Contexte** : Le projet contenait plusieurs fonctionnalités expérimentales ou désactivées :
  - Un module d'inspection multi-photos (`VisionGuardModule`) incompatible avec l'API textuelle de DeepSeek et redondant avec l'inspection humaine.
  - Un faux service de vision (`VisionGuardService`) qui envoyait un second message d'alerte alarmiste 0,5s après l'alerte pour des mots-clés qui doivent être bloqués au matching CPU.
  - Un module de décomposition de lots (`BundleDecompositionModule`) et un service orphelin (`BundleDetector`), alors que `tome_extractor.py` gère déjà les tomes et lots de façon déterministe en CPU.
  - Un module de détection d'anomalie de prix (`PriceGlitchModule`) qui poussait l'IA à halluciner des cotes sans données de marché réelles.
  - Un bouton factice `[ 🛍️ Dressing / Lot (bientôt) ]` sur l'ensemble des alertes Telegram.
- **Décision** :
  - Suppression complète des 5 modules/services et de leurs 2 suites de tests associées (~550 lignes supprimées).
  - Épuration du clavier Telegram à 3 lignes pour les BD (`[ 🖼 Photos ]` `[ 🔗 Annonce ]`, `[ 📊 Prix du marché ]`, `[ 👎 Faux positif ]` `[ 💬 Négocier ]`) et 2 lignes pour les autres catégories.
  - Suppression des flags de configuration désactivés (`AI_FEATURE_VISION_GUARD`, `AI_FEATURE_BUNDLE_DECOMPOSITION`, `AI_FEATURE_PRICE_GLITCH`).
- **Raison** : Focalisation stricte sur le rôle essentiel de Radar Bot : alerte immédiate, matching déterministe infaillible, zéro hallucination et zéro spam.

### ADR-012 : Migration vers l'API interne JSON Vinted (`svc-catalogue`) & Cadence Adaptative
- **Contexte** :
  - Le scraper Vinted téléchargeait les pages de catalogue HTML (`/catalog`) pesant ~7,25 Mo par requête, entraînant une latence réseau de ~2 000 ms et une consommation de bande passante excessive.
  - Le bouton Telegram `[ 🖼 Photos ]` nécessitait une requête HTTP supplémentaire vers Vinted pour extraire la galerie, retardant l'affichage des images.
  - L'intervalle de scrutation fixe (20s) méritait d'être optimisé sans risque de bannissement IP Cloudflare.
- **Décision** :
  - **API Interne First** : Interrogation directe de l'endpoint microservice Vinted Web `https://api.vinted.fr/svc-catalogue/items` avec les en-têtes web actuels (`Platform: web`, `X-Next-App: marketplace-web`, `X-Anon-Id`).
  - **Payload divisé par 190** : Passage de 7,25 Mo à ~38 Ko par appel, réduisant la latence à ~200 ms.
  - **Galerie multi-photos native** : Toutes les photos HD sont extraites dès le scan et stockées dans `listing.photos`. L'ouverture de l'album via `[ 🖼 Photos ]` devient 100 % instantanée (zéro requête Vinted requise).
  - **Résilience absolue (Fallback HTML)** : Si l'API interne retourne une réponse non standard, le scraper bascule automatiquement et de manière transparente sur le catalogue HTML historique.
  - **Cadence Adaptative** : Intervalle de 12s en heures de pointe (11h30-14h00 et 18h00-22h30 Paris) et 15s en heures standard, avec passage automatique à 8s dès configuration d'un proxy résidentiel (Raspberry Pi Zero 2 W via `VINTED_PROXY_URL`).
  - **Ordonnancement Entrelacé (Staggered Interleaving)** : Les catégories hors-livres s'intercalent fluidement avec un espacement de 1,2s à 2,0s, évitant les pics concurrentiels sur une même IP.
- **Raison** : Maximiser la vitesse de détection ("ultra-rapide") pour battre la concurrence tout en préservant l'IP et en garantissant zéro régression.

---

## 3. Registre de Dette Technique Volontairement Différée

Les points suivants sont des éléments de dette connus, assumés et **volontairement non traités**. Sauf demande expresse de l'utilisateur, les assistants ne doivent **pas** tenter de les refactoriser :

| Composant | Dette identifiée | Raison du maintien | Risque en cas de modification |
| --- | --- | --- | --- |
| Handlers Telegram (`alerts_create.py`, `alerts_manage.py`, etc.) | Présence de requêtes SQL ORM directes et d'appels IA dans les contrôleurs aiogram (~900 lignes). | Le code est stable, testé et fonctionne sans régression depuis des mois. | Risque élevé de casser des workflows FSM complexes ou les claviers inline. |
| Matcher ORM vs Déterministe | `IngestListingUseCase` appelle encore `get_matched_alert_ids_for_listing` (service ORM) plutôt que `DeterministicListingMatcher` pur. | Le service ORM applique déjà les règles déterministes tout en résolvant les IDs en base de manière optimisée. | Churn architectural important sans gain de performance mesurable. |
| Deux clients DeepSeek | Coexistence de `DeepSeekClient` (léger, utilisé post-alerte) et `DeepSeekProvider` (utilisé avec Pydantic/Redis). | Chaque client répond à un usage spécifique (streaming/édition rapide vs validation stricte). | Risque de casser les tests unitaires mockés sur l'un des deux providers. |
| Enums doubles | `app/core/constants.py` vs `app/domain/enums.py`. | Préservé pour compatibilité des imports historiques. | Churn massif de commits sur des dizaines de fichiers pour zéro gain fonctionnel. |
| Trois accès Redis | Pool global `get_redis_pool`, stockage FSM aiogram et rate limiter. | Chaque bibliothèque gère son propre cycle de vie de connexion de manière autonome. | Instabilité potentielle du pool ou de la sérialisation des états de session Telegram. |
| Parsing IA en `.get()` | Les modules d'IA post-alerte utilisent `.get("cle")` avec fallbacks plutôt qu'une validation Pydantic stricte. | Résilience accrue aux variations de format JSON retournées par DeepSeek. | Une validation Pydantic stricte provoquerait des rejets d'analyse si le LLM omet un champ mineur. |
| Index composite sur `listings` | Absence d'index `(source_name, received_at)`. | Le volume actuel de listings sur la rétention courte (7-30 jours) n'entraîne aucun ralentissement. | Ajout prématuré d'index inutile à ce stade. |

---

## 4. Règles d'or pour tout Agent LLM

1. **Priorité absolue à la vitesse d'alerte** : Ne jamais insérer un appel réseau (`await httpx`, `await client...`) entre la réception d'une annonce et l'envoi du message Telegram.
2. **Respect strict du processus unique** : Ne jamais ajouter de service worker ou de deuxième scheduler. Tout se passe dans `radar_app`.
3. **Zéro régression sur la suite de tests** : La commande `./.venv/bin/pytest` doit toujours réussir à 100% (320+ tests actifs).
4. **Pas de code orphelin ni de flags morts** : Chaque setting de `app/core/config.py` doit correspondre à une feature vivante et documentée.
5. **Déploiement autonome** : L'assistant exécute lui-même les commandes de déploiement SSH/rsync et les vérifications d'intégrité après chaque intervention validée par l'utilisateur.
