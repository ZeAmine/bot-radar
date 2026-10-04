# BotRadar — Mémoire du projet & Décisions architecturales (`memory.md`)

Ce document consigne la mémoire vivante de **BotRadar**, la genèse de ses choix d'architecture (ADRs), ses arbitrages passés et la liste explicite de sa dette technique acceptée.

Il est destiné à être lu par les modèles de langage (LLMs) et ingénieurs pour comprendre le **pourquoi** derrière les contraintes strictes définies dans [`AGENTS.md`](AGENTS.md) et la cartographie technique de [`subjects.md`](subjects.md).

## Documentation de référence (Univers LLM)
- [`AGENTS.md`](AGENTS.md) : **Constitution & Règles opérationnelles** (Invariants, architecture, déploiement, qualité).
- [`subjects.md`](subjects.md) : **Cartographie des 12 sujets** (Fichiers clés, rôle, flux de données par composant).
- [`memory.md`](memory.md) : **Mémoire du projet & ADRs** (Historique des arbitrages, ADR-001 à ADR-020, dette différée).

---

## 1. Thèse & Philosophie Produit

### Le problème
Sur les plateformes d'occasion (notamment Vinted), les excellentes affaires sur les bandes dessinées de collection, la tech ou les vêtements prisés disparaissent souvent en moins de **60 à 90 secondes**.

### La réponse de BotRadar
BotRadar est un assistant privé d'aide à la décision conçu pour maximiser la vitesse d'acquisition de l'acheteur ("snipe").

```
[ Ingestion Vinted ]
        ↓ (~100ms)
[ Matching Déterministe CPU ]
        ↓ (~2ms)
[ Cote BD (Cache Postgres Local) ]
        ↓ (~1ms)
[ ⚡ ALERTE TELEGRAM INSTANTANÉE ]  <-- Objectif prioritaire : < 500ms
        ↓ (Arrière-plan / non bloquant)
[ MOTEUR IA / Liaison BNF ]
```

### Ce que BotRadar EST
- Un détecteur ultra-rapide d'annonces ciblées.
- Un comparateur de prix instantané par rapport au marché réel de l'occasion livrée.
- Un copilote de négociation et d'évaluation de rentabilité (via IA post-alerte).

### Ce que BotRadar N'EST PAS (et ne sera jamais)
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
  - Tout appel externe (BNF, eBay, moteur IA) est interdit avant `send_listing_alert`.
  - L'enrichissement IA se fait en tâche de fond (`safe_create_task`) et vient modifier ou commenter le message Telegram avec son vrai `message_id`.
  - Si un livre est inconnu en base, sa première alerte part sans cote : le fetch BNF d'arrière-plan enrichira la base pour les alertes suivantes.

### ADR-003 : Matching déterministe local en mémoire CPU
- **Contexte** : Évaluer la pertinence d'une annonce face à des dizaines ou centaines d'alertes utilisateurs.
- **Décision** : Le matching repose sur une normalisation Unicode NFKD rigoureuse, des expressions régulières compilées et des comparaisons scalaires (prix, état, exclusions) en mémoire vive.
- **Raison** : Temps d'exécution unitaire < 100 microsecondes. Fiabilité totale : un mot exclu rejettera toujours l'annonce, là où un modèle vectoriel ou LLM peut halluciner ou laisser passer des faux positifs.
- **Conséquence** : Pas de vector store (Milvus, Chroma, pgvector) ni de LLM sur le chemin critique du matching.

### ADR-004 : Cache local de cote BD avec rafraîchissement bi-quotidien
- **Contexte** : Comparer le prix d'une BD à eBay, Momox, Gibert, Recyclivre, etc.
- **Décision** : La consultation pendant l'alerte lit **exclusivement** la table `book_market_cache`. Le refresh complet interroge les fournisseurs à 07:00 et 19:00 ; un lot incrémental tourne à `:15` et `:45`.
- **Raison** : Protection contre les rate limits des API marchandes et zéro latence réseau lors de l'alerte.

### ADR-005 : Rôle purement consultatif de l'IA (DeepSeek)
- **Contexte** : L'IA apporte une forte valeur ajoutée (conseils de négociation, analyse du prix par rapport à l'état, détection de rééditions). Mais elle tombe en panne : timeout, quota épuisé, JSON illisible.
- **Décision** : L'IA ne bloque jamais le flux principal. Le pipeline IA post-alerte (`AIPostAlertPipeline`) intervient en arrière-plan. En cas d'erreur API, de timeout ou de quota épuisé, l'alerte initiale est déjà entre les mains de l'utilisateur.
- **Conséquence (à ne pas oublier)** : consultatif ne veut pas dire muet. Une liste vide obtenue parce que **personne n'a répondu** ne se confond jamais avec un avis du moteur. Les modules qui rendent un avis portent donc un fait « le moteur a-t-il répondu ? » (`KeywordVariantsOutcome.answered`) et l'interface le dit à l'utilisateur. Une alerte sans variante ni exclusion est créée dans les deux cas ; seule la ligne expliquant pourquoi diffère.

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
  - L'utilisateur utilise systématiquement le bouton principal `[ 🔗 Annonce ]`, qui ouvre l'application Vinted avec ses sessions et moyens de paiement pré-enregistrés.
  - Allègement visuel du clavier Telegram (passage à 3 lignes pour les BD, 2 pour le reste).

### ADR-008 : Auto-Tuning conversationnel & Purge des faux positifs
- **Contexte** : Lorsqu'un utilisateur cliquait sur `[ 👎 Faux positif ]`, l'annonce était enregistrée comme `BotEvent` `listing_disliked`. Cependant, après avoir accepté les exclusions recommandées par l'IA via `/auto-tune`, les faux positifs continuaient d'apparaître dans la liste d'auto-tuning. De plus, l'utilisateur ne pouvait pas ajuster les mots-clés suggérés.
- **Décision** :
  1. Introduction d'une machine à états FSM (`AutoTuneState.waiting_for_custom_exclusions`) permettant à l'utilisateur de copier la suggestion, de la modifier manuellement dans Telegram, et de la soumettre.
  2. Purge atomique des événements `listing_disliked` de l'alerte dès confirmation des exclusions (qu'elles soient appliquées telles quelles ou personnalisées).
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
  - Un module d'inspection visuelle (`VisionGuardModule`) incompatible avec l'API textuelle de DeepSeek et redondant avec l'inspection humaine.
  - Un faux service de vision (`VisionGuardService`) qui envoyait un second message d'alerte alarmiste 0,5s après l'alerte pour des mots-clés qui doivent être bloqués au matching CPU.
  - Un module de décomposition de lots (`BundleDecompositionModule`) et un service orphelin (`BundleDetector`), alors que `tome_extractor.py` gère déjà les tomes et lots de façon déterministe en CPU.
  - Un module de détection d'anomalie de prix (`PriceGlitchModule`) qui poussait l'IA à halluciner des cotes sans données de marché réelles.
  - Un bouton factice `[ 🛍️ Dressing / Lot (bientôt) ]` sur l'ensemble des alertes Telegram.
- **Décision** :
  - Suppression complète des 5 modules/services et de leurs 2 suites de tests associées (~550 lignes supprimées).
  - Épuration du clavier Telegram à 3 lignes pour les BD (`[ 🔗 Annonce ]`, `[ 📊 Prix du marché ]`, `[ 👎 Faux positif ]` `[ 💬 Négocier ]`) et 2 lignes pour les autres catégories.
  - Suppression des flags de configuration désactivés (`AI_FEATURE_VISION_GUARD`, `AI_FEATURE_BUNDLE_DECOMPOSITION`, `AI_FEATURE_PRICE_GLITCH`).
- **Raison** : Focalisation stricte sur le rôle essentiel de BotRadar : alerte immédiate, matching déterministe infaillible, zéro hallucination et zéro spam.

### ADR-012 : Migration vers l'API interne JSON Vinted (`svc-catalogue`) & Cadence Adaptative
- **Contexte** :
  - Le scraper Vinted téléchargeait les pages de catalogue HTML (`/catalog`) pesant ~7,25 Mo par requête, entraînant une latence réseau de ~2 000 ms et une consommation de bande passante excessive.
  - Chaque visite de page d'annonce coûtait une requête HTTP supplémentaire, y compris pour des informations déjà présentes dans le catalogue.
  - L'intervalle de scrutation fixe (20s) méritait d'être optimisé sans risque de bannissement IP Cloudflare.
- **Décision** :
  - **API Interne First** : Interrogation directe de l'endpoint microservice Vinted Web `https://api.vinted.fr/svc-catalogue/items` avec les en-têtes web actuels (`Platform: web`, `X-Next-App: marketplace-web`, `X-Anon-Id`).
  - **Payload divisé par 190** : Passage de 7,25 Mo à ~38 Ko par appel, réduisant la latence à ~200 ms.
  - **Résilience absolue (Fallback HTML)** : Si l'API interne retourne une réponse non standard, le scraper bascule automatiquement et de manière transparente sur le catalogue HTML historique.
  - **Cadence Adaptative** : Intervalle de 12s en heures de pointe (11h30-14h00 et 18h00-22h30 Paris) et 15s en heures standard, avec passage automatique à 8s dès configuration d'un proxy résidentiel (Raspberry Pi Zero 2 W via `VINTED_PROXY_URL`).
  - **Ordonnancement Entrelacé (Staggered Interleaving)** : Les catégories hors-livres s'intercalent fluidement avec un espacement de 1,2s à 2,0s, évitant les pics concurrentiels sur une même IP.
- **Raison** : Maximiser la vitesse de détection ("ultra-rapide") pour battre la concurrence tout en préservant l'IP et en garantissant zéro régression.
- **Mise à jour (2026-09-30)** : `svc-catalogue/items` ne renvoie plus qu'une image de couverture dans `photos[]` (souvent length 1). Le scan n'expose donc plus l'information d'images multiples : seule la couverture est conservée (ADR-015).

### ADR-013 : Passerelle centralisée multi-bots (caddy_gateway) et harmonisation VPS ~/bot-radar/
- **Contexte** :
  - Le VPS héberge désormais plusieurs bots Telegram indépendants (`BotRadar` pour Vinted et `BotStream` pour le streaming de médias).
  - L'ancien conteneur `radar_caddy` monopolisait les ports 80 et 443 de la machine hôte, empêchant l'exposition d'autres applications sans conflit.
  - Le répertoire VPS `~/radar-bot/` créait une discordance visuelle avec `~/bot-stream/`.
- **Décision** :
  - **Passerelle centralisée** : Déploiement d'un service Caddy unique (`caddy_gateway` dans `~/gateway/`) écoutant sur les ports 80/443 de la machine hôte.
  - **Réseau partagé `web_gateway`** : Création d'un réseau Docker externe reliant `caddy_gateway` à `radar_app:8000` et `botstream_app:8080`.
  - **Épuration de `compose.yml`** : Suppression du conteneur `radar_caddy`, suppression de `Caddyfile` local devenu code mort, attachement de `app` au réseau `web_gateway`.
  - **Harmonisation VPS `~/bot-radar/`** : Renommage du répertoire de projet sur le VPS vers `~/bot-radar/`, maintien d'un symlink `~/radar-bot -> ~/bot-radar` et mise à jour du crontab de backup automatique.
  - **Zéro perte de données** : Duplication des volumes Docker vers `bot-radar_postgres_data` et `bot-radar_redis_data` avec snapshot SQL préalable.
- **Raison** : Architecture serveur propre, modulaire et extensible permettant d'héberger plusieurs bots en parallèle sur un même VPS sans conflit de ports ni interruption de service.

### ADR-014 : La BNF est la seule autorité ISBN (« l'IA propose, la BNF dispose »)
- **Contexte** : `/add la couleur des choses. Max 12 €` proposait l'ISBN `9782369903062` pour l'ouvrage « La couleur des choses » de Martin Panchaud. Cet ISBN est **réel et valide** : il appartient à « Keeping two » de Jordan Crane, même éditeur (Çà et là), même année. DeepSeek n'a pas inventé des chiffres, il a recombiné un préfixe d'éditeur plausible. Un contrôle de clé de contrôle ne l'attrape pas.
  - **Impact** : un ISBN faux n'est pas bénin. Il devient une contrainte de matching **stricte** (`listing_match.py`), l'URL de recherche Vinted (`search_start_type=item_page_isbn`) et la clé de cote marché. L'alerte ne peut alors plus jamais matcher, et sa ligne de prix compare deux livres différents.
- **Décision** :
  - `extract_clean_isbn` valide désormais la clé de contrôle (ISBN-10 mod 11, ISBN-13 pondérée 1/3) et renvoie `None` sur un code mal saisi. Filet anti-typo sur `/addisbn`, `/edit` et les URLs.
  - `BookMetadataService` gagne trois entrées : `resolve_for_alert(titre, auteur)` (notice de référence + éditions alternatives), `verify_isbn(isbn, titre)` (confirme le **couple** ISBN ↔ œuvre) et `title_similarity` / `same_author`.
  - `natural_add` ne décide plus de l'ISBN : il propose des candidats (ISBN saisi par l'utilisateur, puis ISBN du modèle) et n'en retient un que s'il est confirmé par la BNF pour l'œuvre demandée. Sinon l'alerte part sur le titre avec un avertissement visible dans le récap.
  - Le prompt de `natural_add` n'interdit plus de proposer un ISBN mais exige la recopie de celui écrit par l'utilisateur ; il ne liste plus d'exclusions figées.
  - Le prompt de `keyword_variants` est ancré sur la fiche réelle (`BookContext` : auteur, éditeur, année, format, ISBN) et interdit les listes de goodies génériques. Le même appel sert au récap de `/add` et au bouton `[ ✨ Variantes & Exclusions IA ]`.
  - `/edit` refuse un ISBN que la BNF rattache à un autre ouvrage, en nommant le vrai titre. Une panne réseau ne bloque jamais la saisie (absence de preuve = pas de refus). **La création d'alerte applique la même règle depuis ADR-021** : `realign_draft_to_isbn` distingue les trois verdicts, là où `lookup_by_isbn` sans contrôle de titre laissait passer l'ISBN d'un autre livre.
  - **Règle de sélection d'édition** : similarité de titre (poids 3) + concordance d'auteur + bonus « notice datée ». Une notice sans date est un doublon d'édition : elle sert d'alternative, jamais de référence. Une alternative doit partager le titre **et** l'auteur, sinon « De la couleur des choses » (Casabianca) serait proposé comme édition de « La couleur des choses » (Panchaud).
  - **Repli OpenLibrary (autorité secondaire)** : quand la BNF ne renvoie aucune notice exploitable, l'API Search d'OpenLibrary est interrogée (`_openlibrary_works`). Trois règles encadrent ces données :
    - `source="openlibrary"` et `confidence=MEDIUM` ; le récap affiche « ℹ️ Source OpenLibrary (repli BNF) » pour que l'utilisateur puisse exiger la BNF.
    - **Jamais en override** : si la BNF connaît l'ISBN mais que le titre ne correspond pas, c'est une preuve de contradiction — OpenLibrary n'est pas consulté et ne peut pas réinstaller ce code.
    - **Sélection d'ISBN dans un document agrégé** : OpenLibrary regroupe toutes les éditions d'une œuvre (y compris les traductions) dans un seul document. On retient le code demandé, puis un ISBN-13, puis une édition francophone (`9782`/`979`). Les autres ISBN francophones du document deviennent les éditions alternatives.
  - **Limite assumée** : OpenLibrary ne connaît pas les ouvrages absents de son index (ex: une première édition française introuvable). Dans ce cas l'alerte part sur le titre, avec l'avertissement habituel.
- **Raison** : Le matching doit rester déterministe et sans hallucination. Une donnée d'identité fausse est le seul type d'erreur qui contamine silencieusement tout le chemin aval (alerte, cote, négociation).
- **Conséquence assumée** : `/add` effectue deux appels au moteur IA (parsing puis mots-clés, ce dernier relancé une seule fois si l'appel échoue) et une résolution bibliographique, uniquement en phase de création — jamais avant une alerte (invariant n°1 intact). Un livre absent de la BNF **et** d'OpenLibrary est surveillé par titre au lieu d'être ignoré. Le repli s'applique aussi à `_link_book_from_bnf`, tâche de fond post-alerte : l'alerte est déjà partie, l'invariant n°1 n'est pas concerné.

### ADR-015 : Suppression de l'album d'images (l'alerte garde sa couverture)
- **Contexte** : L'album d'images coûtait une requête Vinted authentifiée par annonce (page `/items/{id}`) au clic, plus un préchargement systématique en tâche de fond après chaque alerte notifiée. Le scan `svc-catalogue` ne fournissant de toute façon qu'une couverture, ce coût réseau ne rapportait presque rien.
- **Décision** : suppression complète de la fonctionnalité — bouton, handler de callback, suppression de l'album, préchargement, fonction de récupération réseau, extraction associée, champ de galerie dans le parsing, et toute mention documentaire.
  - Clavier d'alerte : ligne 1 réduite à `[ 🔗 Annonce ]` ; 3 lignes pour les BD, 2 pour les autres catégories.
  - Le parsing ne conserve que la photo de couverture ; le payload d'ingestion et le schéma API n'acceptent qu'une image.
  - Une branche de code morte conditionnée par la présence d'images (`raw_payload["identified_tome"]`, clé jamais écrite) a été supprimée au passage.
- **Raison** : une fonctionnalité qui coûte une requête réseau par alerte pour un contenu presque toujours indisponible est du code mort de production. La couverture reste : c'est elle qui donne l'information au moment de la décision d'achat.
- **Conséquence assumée** : l'utilisateur doit ouvrir l'annonce Vinted pour voir d'éventuelles autres images.

### ADR-016 : Un seul filtre d'occasion, une seule référence de prix
- **Contexte** : audit du chemin « cote de marché ». Quatre défauts produisaient des cotes fausses ou trompeuses.
  1. `extract_best_historical_low` faisait `int(raw)` sans garde : une valeur nulle ou non numérique faisait échouer le refresh d'un livre entier.
  2. Le registre (`ProviderRegistry.select_best_offer`) ne filtrait ni l'état ni le Neuf : une offre Recyclivre en « Bon état » pouvait battre une offre Chasse-aux-livres en « Très bon » et devenir la cote.
  3. `ChasseAuxLivresProvider.is_title_coherent` acceptait **un seul mot commun** (`len(common) > 0`), contenait un dictionnaire d'œuvres figé (`montagnes hallucinees`, `appel de cthulhu`, `homme sans peur`) et renvoyait `True` par défaut quand il n'avait rien à comparer.
  4. Le résumé d'alerte et le panneau détaillé ne citaient pas la même référence : le résumé comparait au prix le plus bas 1 an, le panneau écrasait cette valeur par le prix du jour. Le lot cumulait les cotes de tomes non appariés.
- **Décision** :
  - **`app/domain/rules/market_offer.py`** : règles pures de classification d'une offre (`condition_tier`, `is_very_good_or_better`, `is_acceptable_secondhand_condition`, `is_brand_new_partner`, `is_merchant_excluded`, `is_comparable_secondhand_offer`). Un état non qualifié (« Occasion ») reste acceptable ; un état déclaré et inférieur à « Très bon » est refusé. Une seule définition, partagée par le registre, le fournisseur et l'affichage.
  - **Référence unique** : `reference_price(cache)` renvoie le prix le plus bas constaté sur 1 an s'il existe, sinon le prix du marché du jour. L'écart compare le total Vinted **frais de port inclus** (`_vinted_total_cents`) à cette référence, dans le résumé comme dans le panneau.
  - **Titre** : `is_title_coherent` s'appuie sur `title_similarity` (seuil 0,72), refuse deux tomes différents et exige un noyau d'œuvre identique ; plus de dictionnaire figé, plus de `True` par défaut.
  - **ISBN** : `normalize_search_isbn` valide la clé de contrôle dans le registre avant tout appel réseau ; un ISBN invalide fait basculer la recherche sur le titre au lieu de coter l'ouvrage d'un autre livre.
  - **Lot** : seuls les tomes appariés sont cumulés, la couverture réelle est annoncée (`Lot, 2/3 tomes`).
  - **Lot dénombré** : `TomeExtractor` remontait déjà `lot_count` pour « lot de 4 tomes », mais la résolution exigeait `lot_tomes` et jetait ce compte : l'alerte retombait sur « cote indisponible » alors que le catalogue portait les quatre tomes. `resolve_loaded_books` compose désormais le lot **uniquement** si la série compte exactement ce nombre de tomes identifiables et sans doublon d'édition, et marque le résultat `composition_inferred` pour que l'affichage dise « composition déduite » au lieu de « (Lot) ». Refusé si la série est plus longue, si un tome a deux éditions, ou si aucune fiche n'est numérotée : sans preuve, mieux vaut la ligne honnête d'impuissance qu'une cote composée de travers.
  - **Refresh** : vagues concurrentes bornées (4), pause anti-rate-limit une fois par vague au lieu d'une fois par livre, plafond de durée de passe, persistance dédupliquée en une fonction pure (`BookPricingResult`).
- **Raison** : une cote fausse fait perdre de l'argent ou fait manquer une vraie bonne affaire. Mieux vaut une cote absente qu'une cote attribuée au mauvais ouvrage, au mauvais tome ou au mauvais état.
- **Conséquence assumée** : le provider est plus strict sur les titres ; une correspondance auparavant acceptée au premier mot commun est désormais refusée, ce qui peut laisser certains livres sans cote.

### ADR-017 : Choisir son édition dans le récap, avant de créer l'alerte
- **Contexte** : ADR-014 a rendu la BNF seule autorité ISBN, mais le choix de l'édition restait invisible. `resolve_for_alert` renvoyait bien des éditions alternatives, mais elles n'étaient réduites qu'à une ligne de texte dans l'ISBN (`isbn_note`). L'utilisateur ne pouvait corriger un ISBN qu'en annulant et en recommençant `/add`. Or l'ISBN engage tout : recherche Vinted (`search_start_type=item_page_isbn`), contrainte de matching stricte et clé de cote de marché. Deux éditions du même album (Initiale parue en 2022, réédition de 2018) donnent deux marchés différents.
- **Décision** :
  - `BookResolution.editions()` est l'unique source des éditions concurrentes : `EditionAlternative` (ISBN, éditeur, année, format), édition de référence en tête, doublons supprimés. `natural_add` l'expose dans `NaturalAddResult.edition_alternatives` ; la saisie manuelle appelle `resolve_for_alert` une fois de plus.
  - `format_semi_auto_recap` affiche l'édition retenue sur une ligne « 📖 Édition suivie » (millésime · éditeur · format) ; `get_alert_confirmation_keyboard` place un bouton par édition **avant** le bouton de création (`al_create_edition:{index}`), numéroté `1️⃣`/`2️⃣` comme cette ligne, avec le libellé `Éditeur - année`. Les deux n'apparaissent qu'**à partir de deux éditions exploitables** : sous ce seuil, il n'y a rien à choisir et le récap décrit déjà l'édition par sa ligne « 🏢 Éditeur ».
  - `select_alert_creation_edition` bascule l'ISBN, l'éditeur, et force `is_series=False` puis rend le récap à jour. Un ISBN saisi à la main dans le menu ✏️ réaligne la liste.
  - Nettoyage au passage : les six appels dupliqués à `format_semi_auto_recap` sont remplacés par `recap_from_state(draft)` / `creation_keyboard(draft)`, et `format_edition_hint` (module IA) fusionne dans `format_edition_fragments` (service de métadonnées).
- **Raison** : l'ISBN est une décision, pas une formality. Montrer les alternatives au moment du récap — où l'annulation est encore gratuite — coûte une requête BNF de plus et supprime le seul cas où l'utilisateur créait une alerte sur la mauvaise édition.
- **Conséquence assumée** : une requête SRU BNF supplémentaire par création d'alerte BD (chemin utilisateur, hors chemin d'alerte). Un échec réseau fait simplement disparaître le bloc, sans jamais bloquer la création.

### ADR-018 : Le fournisseur IA est un adaptateur, jamais une dépendance

### Statut
Accepté et mis en œuvre.

### Contexte
Le moteur IA était cousu au projet par dix points : les noms de variables `DEEPSEEK_*`, le singleton `deepseek_client` importé par huit fichiers, le format de requête et de réponse OpenAI, la sonde `GET /models`, quatre exceptions nommées `DeepSeek*` dans un fichier partagé, un préfixe de cache Redis, quatre textes utilisateur, les messages de log, et quatre fichiers de tests nommant le fournisseur. Changer de modèle — le cas le plus probable, un modèle moins cher ou plus récent sort — demandait onze fichiers et cassait quatre tests.

Deux défauts ne se seraient pas manifestoés avant la bascule : la clé de cache d'analyse ne contenait pas le modèle (`radar:deepseek:analysis:{digest}`), donc le nouveau modèle n'aurait jamais été consulté et l'ancien résultat aurait été servi en silence pendant sept jours ; et `app/providers/llm/deepseek_provider.py` importait `app/services/ai/client.py`, c'est-à-dire un provider HTTP dépendant d'un service.

### Décision
`app/providers/llm/` devient la seule frontière qui connaît un fournisseur (invariant n°4 de `AGENTS.md`).

- `base.py` porte le contrat `LLMClient` et la classe `BaseLLMClient`, qui concentre la sémantique commune : activation, client HTTP partagé, absorption des erreurs, repli `None`, sonde de santé en cache 30 secondes. Un adaptateur n'écrit que cinq méthodes de traduction filaire.
- Deux adaptateurs sont livrés : `openai_compatible` (DeepSeek, Groq, Mistral, OpenRouter, Ollama, LM Studio) et `anthropic`, dont l'API Messages ne partage rien avec le dialecte OpenAI. Le second existe pour prouver la frontière : s'il passe le contrat sans qu'une ligne au-dessus change, la couture est au bon endroit.
- `registry.py` fait la bascule par `AI_PROVIDER`, et lève sur un fournisseur inconnu : une faute de frappe doit se voir au démarrage, pas éteindre l'IA sans rien dire.
- Variables neutres `AI_PROVIDER`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`, `AI_TIMEOUT_SECONDS`, sans shim de compatibilité : le `.env` de production a été renommé sur place, la valeur de la clé n'a jamais été affichée.
- `ListingAnalysisProvider` (ex `DeepSeekProvider`) est conservé et rendu neutre ; sa clé de cache porte désormais fournisseur, modèle et version de prompt.
- Les exceptions `DeepSeek*` deviennent `LLM*` dans `app/core/exceptions.py`.
- `tests/unit/test_llm_contract.py` rejoue le même contrat sur chaque adaptateur enregistré, transmission des images comprise : la promesse « une variable d'environnement » est vérifiée, pas supposée.

### Raison
Le projet envoie déjà des alertes en quelques dizaines de millisecondes ; l'IA est censée suivre le marché, pas le freiner. Un modèle qu'on veut remplacer en cinq minutes ne doit pas demander une journée, ni risquer de casser un handler de négociation.

### Conséquence assumée
Un fournisseur au dialecte inconnu demande un fichier dans `app/providers/llm/` et une ligne dans `LLM_ADAPTERS`. C'est le coût du contrat. Chaque adaptateur ajouté augmente la surface du contrat de test : c'est le prix à payer pour savoir que l'adaptateur fonctionne avant de le brancher en production.

---

## 3. Décisions qui ne sont pas de la dette

Ces points ont été décrits comme de la dette à ne pas toucher. Le code dit autre chose.

- **Enums.** Une seule définition dans `app/domain/enums.py`, réexportée par `app/core/constants.py`.
- **Index `(source_name, received_at)`.** Aucune requête ne l'utilise. Le doublon est `(source_name, external_id)`.
- **Moteur IA.** Un seul POST, `BaseLLMClient.complete`. `ListingAnalysisProvider` ajoute Pydantic et le cache Redis, hors ingestion. Le fournisseur est un adaptateur (ADR-018), pas une dépendance.
- **Matching.** Les règles sont dans `app/domain/rules/listing_match.py`. L'ingest appelle `evaluate_listing_against_alert`. Il n'y a pas de second algorithme ORM.
- **JSON IA.** Chaque module valide un modèle Pydantic dont les champs manquants prennent un défaut.
- **SQL.** Les lectures encore dans les handlers sont passées aux repositories (`stats_repo`, `alert_repo`, `book_repo`, `autotune_repo`).
- **Redis.** Le rate limiter et le FSM Telegram partagent le client de `app.core.redis`.

### ADR-019 : Une cote « prix le plus bas constaté » bornée par nos propres relevés
- **Contexte** : une alerte BD annonçait « Prix bas constaté : 22,90 € » pour *Dead Train*, un ouvrage que l'agrégateur lui-même affichait à 13 € et quelques. Deux défauts distincts se cumulaient.
  1. **La règle de sélection** (`select_historical_low_cents`) renvoyait la **première** série d'état trouvée parmi `("Très bon état +", "Comme neuf +")` au lieu du **minimum** de ces séries. Or les deux paliers sont des occasions comparables (`is_very_good_or_better` accepte `like_new` **et** `very_good`) : un prix « Comme neuf » plus bas doit entrer dans le plus bas constaté. Le résultat était un « prix le plus bas » situé **au-dessus** d'un prix déjà constaté dans un état meilleur — une incohérence logique, pas une approximation.
  2. **La valeur ne pouvait plus être corrigée.** `/rest/jcd` de Chasse-aux-livres ne répond plus depuis le 2026-09-11 (le site exige le jeton anti-bot ; `CHASSE_AUX_LIVRES_DUIH` est vide en production). 230 snapshots sur 2 721 portaient encore un historique 1 an, tous dans une fenêtre de 12 h. Depuis, chaque passe renvoie `None` et `update_or_create_cache` préserve l'ancien chiffre **en réécrivant `historical_low_at = now`** : une valeur de trois semaines se présentait comme vérifiée la veille.
  - Contrôle en base : **9 livres** portaient une cote 1 an *supérieure* à un prix livré que nous avions nous-mêmes enregistré pour ce même livre — Dead Train 22,90 / 13,57, Carcajou 13,11 / 3,90, Train de nuit 14,52 / 5,92, Watership Down 23,99 / 18,22. Conséquence utilisateur : une annonce Vinted à 20,38 € était présentée « ✅ −2,52 € » alors que l'ouvrage s'écoulait 13,57 €.
- **Décision** :
  - **`select_historical_low_cents`** renvoie le minimum des séries dont le libellé est accepté. L'ordre des libellés ne change plus le résultat.
  - **`MarketPriceRepository.clamp_historical_low_by_observations`** : à chaque écriture de cote, la valeur est bornée par le plus bas prix livré de nos snapshots comparables (`get_observed_low_cents`, offre vide `delivered_price_cents = 0` exclue). Le même bornage s'applique au chemin de résilence de `persist_market_result` : un silence réseau n'empêche pas de réparer une cote surestimée.
  - **Le plancher corrige, il ne crée pas.** Sans cote préexistante, la référence reste le prix du marché du jour : sinon les 32 livres déjà relevés basculeraient en « prix bas constaté » pour leur seul prix du jour.
  - **Migration `0005_correct_historical_low_from_observations`** : n'abaisse que, ne crée rien, idempotente. `downgrade` ne fait rien plutôt que de restaurer une valeur inconnue.
  - **Libellés** : « Prix bas constaté (1 an) » devient « Prix bas constaté », et le panneau « Cote d'occasion basée sur le plus bas prix que nous avons pu constater ». La ligne d'alerte, elle, ne change pas.
- **Raison** : une cote fausse fait manquer une vraie bonne affaire — c'est exactement ce que l'utilisateur a constaté. Un plancher cheap, local et monotone vaut mieux qu'un agrégateur tiers dont la série 1 an peut s'arrêter sans un seul signal.
- **Conséquence assumée** : tant que `CHASSE_AUX_LIVRES_DUIH` reste vide, la série certifiée du site ne revient pas et la cote vient de nos relevés (rétention 90 j). La ligne n'affiche toujours aucune date : un chiffre sans date reste un chiffre sans date.

### ADR-020 : Une cote qu'on ne nourrit plus se signale, et la session se réinjecte
- **Contexte** : deux suites à l'incident « 22,90 € » (ADR-019). Rien n'avertissait qu'une cote cessait d'être nourrie — `historical_low_at` étant réécrit à chaque passe, un chiffre mort se lisait comme vérifié hier. Et la source qui devait le corriger, `/rest/jcd` de Chasse-aux-livres, n'est revenue sur aucun jeton : elle répond `200` vide à une requête HTTP seule et exige une session ayant passé le défi anti-bot du site.
- **Décision** :
  - **Signal** : `app/services/market_health.py` liste, une fois par jour au plus, les livres d'alerte BD active dont la cote affichée n'est nourrie par aucun relevé certifié depuis sept jours. Il regarde ce qui a été **observé**, pas ce qui est affiché. Dédoublonné par Redis (`SET NX`), il ne lève jamais, et le job de rafraîchissement l'appelle après sa passe, dans sa propre session.
  - **Session** : `resolve_session_token()` lit l'environnement d'abord, Redis ensuite. `scripts/sync_chasse_session.py` prend le jeton (argument, fichier ou variable), **le valide en interrogeant `/rest/jcd` avec le provider de production**, et ne l'injecte qu'après une réponse non vide — via `POST /api/v1/internal/chasse/session`, qui ne renvoie que la longueur. `GET` sur la même route dit `has_token` / `source` sans jamais exposer la valeur.
  - **Pré-vol** : avant synchro, la base est comparée à la tête du graphe **du conteneur qui tourne**, plus à la tête locale : exiger la révision locale rendait tout déploiement d'une migration impossible, le rebuild précédant l'`upgrade head`. Reste fatal : schéma en avance ou en retard sur le code servi, plusieurs têtes en production, et — après synchro — base ne portant pas la tête locale.
- **Raison** : un chiffre faux que rien ne contredit devient une décision d'achat fausse. Le signal et le injectable traitent les deux moitiés : celle qui prévient, celle qui répare.
- **Conséquence assumée** : le jeton doit être capturé à la main dans un navigateur (le site ne le délivre qu'à une session ayant passé son défi) ; le script valide avant d'injecter plutôt que de promettre. La capture automatique d'un défi JavaScript n'est pas dans le dépôt.

### ADR-021 : Un homonyme n'est pas l'œuvre, et changer d'ISBN change d'ouvrage
- **Contexte** : `/add les enfants perdus` (BD de Nef, parue en 2025) produisait un récap entièrement rempli de données du **roman de 1989** de Rosemonde Ahou de Saintange, sous l'ISBN que l'utilisateur venait de saisir à la main. Cinq défauts s'enchaînaient.
  1. `resolve_for_alert` ne filtrait que sur `title_similarity >= 0.75`. Un titre identique vaut 1.0 : l'homonyme passait pour l'œuvre demandée. Le score BNF ne départageait deux homonymes que de quelques dixièmes, et l'indice d'auteur ne pesait que `-0.5`.
  2. `_is_same_work` **écartait** toute notice d'auteur différent : l'ambiguïté était supprimée au lieu d'être montrée.
  3. `natural_add` faisait primer l'auteur deviné par le moteur sur la notice BNF (`author or book.author`), à l'envers du commentaire qui le précédait.
  4. La fiche homonyme alimentait le `BookContext` du module de mots-clés : le moteur raisonnait sur le roman et produisait « les enfants perdus 1989 », « les enfants perdus la pensée universelle », « les enfants perdus 9782214080627 ».
  5. Une variante est une **porte de sortie** du matching (`listing_match.py` : une seule variante suffit à valider une annonce) : ces trois chaînes faisaient sonner l'alerte BD sur les annonces du roman.
- Et surtout : **corriger l'ISBN ne réalignait rien**. Le chemin de saisie appelait `lookup_by_isbn` sans contrôle du titre (ADR-014 imposait `verify_isbn`, `/edit` le faisait déjà, la création non) ; l'auteur et l'éditeur n'étaient repris que s'ils étaient non nuls ; variantes, exclusions, `book_context` et éditions restaient ceux de l'ancien ouvrage ; `isbn_note` était effacé, si bien que l'ISBN saisi s'affichait comme confirmé par la BNF. Un ouvrage paru la semaine dernière n'étant pas encore catalogué, `meta` valait `None` : l'auteur de 1989 restait sous l'ISBN de 2025. La ligne « 🚫 Exclus » disparaissait sans explication — `_keyword_note` ne disait rien dès que le moteur renvoyait *au moins une variante* — et le bouton de relance disparaissait avec (`keywords_finalized`).
- **Décision** :
  - **`BookResolution.other_works` + `is_ambiguous`** : les notices de **même titre** (titre normalisé identique à celui demandé) et d'auteur différent remontent au lieu d'être jetées. L'ambiguïté porte sur le titre saisi : « Les Enfants perdus tome 2 » n'est pas rendu ambigu par « Les Enfants perdus », pas plus que « De la couleur des choses » par « La couleur des choses ». Un **indice d'auteur** — y compris celui écrit par l'utilisateur après « de », nettoyé de sa ponctuation — désigne la notice (`matched`), la contredit (`conflicts`) ou ne tranche rien (`none`) ; seul `matched` résout l'homonymie.
  - **Tant que c'est ambigu, rien n'est rattaché** : ni ISBN, ni auteur, ni éditeur, ni fiche IA, ni éditions. Le récap porte « 🔢 ISBN : non confirmé par la BNF » et l'avertissement (`ambiguity_note`), et le clavier propose un bouton par œuvre (`al_create_work:`, libellé « 1️⃣ Auteur · année · éditeur », **avant** les éditions : d'abord quel livre, puis lequel). La saisie manuelle applique la même règle.
  - **La BNF prime sur le moteur** : quand une notice est retenue, c'est son auteur et son éditeur qui s'affichent.
  - **`confirm_isbn_for_title`** remplace le double verdict implicite de `verify_isbn` : il rend un `IsbnVerdict` dont l'issue est `confirme`, `contradiction` (les autorités rattachent cet ISBN à un autre ouvrage), `inconnu` (personne ne connaît ce code) ou `silence` (personne n'a pu répondre). `verify_isbn` en est le cas particulier : il ne rend la notice que sur un verdict confirmé.
  - **`realign_draft_to_isbn`** (création) réécrit toute la fiche d'identité : confirmation → la notice fait autorité ; contradiction → **refus motivé**, en nommant le vrai titre et son auteur ; silence → ISBN gardé, identité précédente vidée, note « ISBN saisi, non confirmé par la BNF » ; **panne → comme le silence**, la note disant que la BNF n'a pas pu être interrogée. `IsbnConfirmation` porte `panne` : `confirm_isbn_for_title` ne retient plus la notice de repli quand la BNF n'a pas répondu, sinon le brouillon se réalignait sur une confirmation que personne n'avait faite — et `verify_isbn`, même fonction, ne valide plus rien sur une panne. Changer d'ISBN, c'est changer d'ouvrage : les variantes, exclusions et la fiche IA de l'ancien ouvrage sont invalidés (`KEYWORDS_FROM_PREVIOUS_WORK`) et la recherche repart seule en arrière-plan, par le chemin déjà éprouvé du `/add` (`relaunch_keyword_search`). Les mots-clés d'inclusion, eux, survivent : ils dérivent du titre, pas de l'œuvre.
  - **Variantes** : `_clean_terms` écarte tout terme porteur d'une suite de chiffres longue (ISBN avec ou sans tirets) et tout terme qui répète le titre ; le prompt l'interdit explicitement. Le matching applique la même règle **en lecture** (`usable_variants`) : une variante qui cite un ISBN différent de celui de l'alerte ne peut pas l'aider à trouver son ouvrage. Les alertes déjà enregistrées en profitent, sans migration.
  - **`_draft_matches`** compare désormais aussi `isbn` et `author` : un résultat de recherche devenu étranger est jeté.
  - **Audit des alertes enregistrées** (`scripts/audit_alerts_identity.py`, ADR-021) : la règle de création est confrontée, après coup, à ce qui est en base. `inspect_identity` est pure et réutilise les seuils de `book_metadata_service` ainsi que `carries_foreign_isbn` : l'audit et le matching ne peuvent pas diverger sur ce qu'est un autre ouvrage ou une variante étrangère. Le script ne corrige rien — les mots-clés ont pu être retouchés à la main — et une panne réseau met l'alerte de côté au lieu de la compter en anomalie Comme `lookup_by_isbn` absorbe ses propres erreurs et renvoie `None`, `lookup_by_isbn_verdict` distingue « notice absente » de « autorité muette » (`IsbnLookup`), sans rien changer à `lookup_by_isbn` pour ses appelants : une panne isolée met l'alerte de côté au lieu d'accuser un ISBN d'être inconnu. `bnf_joignable` reste, pour le cas global : aucune notice n'est demandée et la sortie est 2.
  - **Une panne ne se remplace pas par un repli** : quand la BNF ne répond pas mais que OpenLibrary répond, le verdict est `silence` — il porte la notice du repli **et** le motif de la panne. Sans cela, une indisponibilité et un refus de la BNF donnaient deux verdicts strictement identiques, et le rapport annonçait « OpenLibrary a répondu, pas la BNF » — un refus pour une panne. `panne` l'emporte donc dans `audit()` : l'alerte est mise de côté, la notice de repli est écartée, et le motif le dit.
  - **Un seul verdict, une seule liste d'autorités** : `IsbnLookup` et `IsbnConfirmation` ont disparu au profit d'`IsbnVerdict` (`outcome`, `notice`, `consulted`, `panne`), dont `__post_init__` refuse toute combinaison contradictoire — trois champs facultatifs admettaient « aucune autorité n'a répondu » à côté d'une autorité nommée. `consult_authorities` est le seul chemin qui interroge les autorités : c'est donc le seul qui puisse écrire, dans l'ordre des appels, la liste `consulted`, que « ni la BNF ni OpenLibrary » n'a plus à deviner. Les trois champs optionnels de `AlertVerdict` ont cédé la place à ce verdict : l'audit ne peut plus écrire un motif de non-vérification à côté d'une autorité qui a tranché.
  - **Le rapport nomme l'autorité qui a répondu** : `consult_authorities` se replie sur OpenLibrary quand la BNF ignore l'ISBN, et l'audit complet l'a mesuré (92 verdicts BNF, 1 OpenLibrary, 4 sans autorité sur 98 alertes). Les messages `isbn_conflit`, `titre_divergent`, `auteur_divergent` et `editeur_divergent` lisaient « la BNF » en dur : un verdict rendu par l'autorité secondaire s'affirmait donc attribué à la BNF. L'autorité se lit désormais sur `BookMetadata.source` (`libelle_autorite`) et chaque verdict la porte (`AlertVerdict.autorite`), y compris dans le `--json`. Un repli ne reçoit jamais le marqueur `ok` : ligne `REPLI`, ligne de résumé à part, et le verdict « conforme » est suspendu tant qu'un repli existe. `isbn_inconnu` nomme les deux autorités réellement interrogées — sauf ISBN mal formé, où personne n'a été demandé et où l'accusation serait inventée. **Les codes de sortie restent 0/1/2** : un repli n'est pas une anomalie d'identité, c'est une absence de confirmation, et rien ne change pour qui automatise le script.
  - **Le rapport est daté et nommé** : le bot créant des alertes pendant que l'audit tourne, deux exécutions de la même commande ont des dénominateurs différents à juste titre — 97, puis 98, puis 99 alertes actives. « 24 anomalies » n'était donc opposable à personne. Chaque rapport porte désormais son en-tête : l'instant de lecture de la population (`population_lue_le`, et non l'heure de fin, qui dépend de la lenteur des autorités) et l'effectif audité, inactives comprises. Le `--json` cesse d'être un tableau nu pour un objet `{genere_le, population, alertes}` : sans cet en-tête, sa population resterait sans nom et un producteur automatisé devrait le deviner. `--quiet` garde l'en-tête — il retire les lignes `ok`, pas ce qui rend le rapport comparable — et une base vide reste un rapport daté, en JSON comme en texte.
  - **`_keyword_note`** couvre les cinq fins (les deux listes, aucune exclusion, aucune variante, aucune des deux, moteur muet).
- **Raison** : l'ISBN engage le matching, l'URL de recherche Vinted et la clé de cote. Une fiche d'identité fausse ne se corrige pas après coup — c'est pour cela qu'elle ne doit jamais s'afficher sans preuve, et qu'une correction doit réécrire ce qu'elle contredit au lieu de le laisser survivre.
- **Conséquence assumée** : un ouvrage que la BNF ignore ne peut être suivi qu'après que l'utilisateur a lui-même saisi son ISBN, et son auteur ne s'affiche plus tant qu'aucune autorité ne le confirme. Une requête SRU supplémentaire quand on corrige un ISBN (déjà le cas à la création d'après ADR-017), et une résolution de titre au lieu de deux requêtes sur la saisie manuelle.

---

## 4. Règles d'or pour tout Agent LLM

Les invariants n°1 (alerte Telegram d'abord) et n°2 (un seul scheduler) ne sont pas répétés ici :
ils sont définis une seule fois, avec leurs critères de vérification, dans l'invariant
correspondant d'`AGENTS.md`. Ce qui suit n'est pas une copie de la constitution.

1. **Un seul endroit par règle** : une règle dure vit dans `AGENTS.md` ; un ADR la justifie, un sujet la cartographie. Ne pas la redéclarer ailleurs sous une forme plus lâche.
2. **Zéro régression sur la suite de tests** : `./.venv/bin/pytest` doit réussir à 100 % (compteur courant : voir le badge du README), et chaque commit doit passer seul — `make check-history`.
3. **Pas de code orphelin ni de flags morts** : chaque setting de `app/core/config.py` doit correspondre à une feature vivante et documentée.
4. **Déploiement autonome, une fois validé** : après une intervention validée par l'utilisateur, exécuter soi-même les commandes SSH/rsync et les vérifications d'intégrité, sans lui demander de copier-coller. Jamais spontanément, et jamais pour une opération destructive non demandée.
