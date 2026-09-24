# Radar Bot 🛰️

Radar Bot est un bot Telegram privé, temps réel et hautement disponible, spécialisé dans la détection et l'aide à la décision pour les bonnes affaires d'articles de seconde main.

Il surveille trois univers verticaux à forte valeur ajoutée :
- 📚 **BD & Comics** (avec comparaison automatique du prix du marché de l'occasion livrée)
- 👕 **Vêtements**
- 💻 **Tech**

---

## Sommaire

1. [Présentation de Radar Bot](#1-présentation-de-radar-bot)
2. [Fonctionnalités de la V1](#2-fonctionnalités-de-la-v1)
3. [Limites de conformité et éthique](#3-limites-de-conformité-et-éthique)
4. [Prérequis techniques](#4-prérequis-techniques)
5. [Création du bot avec BotFather](#5-création-du-bot-avec-botfather)
6. [Comment obtenir son identifiant Telegram](#6-comment-obtenir-son-identifiant-telegram)
7. [Création des clés eBay Developer](#7-création-des-clés-ebay-developer)
8. [Installation locale](#8-installation-locale)
9. [Configuration des variables d'environnement (.env)](#9-configuration-des-variables-denvironnement-env)
10. [Lancement avec Docker Compose](#10-lancement-avec-docker-compose)
11. [Gestion des migrations Alembic](#11-gestion-des-migrations-alembic)
12. [Définition et activation du Webhook Telegram](#12-définition-et-activation-du-webhook-telegram)
13. [Exemple curl d'ingestion d'annonce](#13-exemple-curl-dingestion-dannonce)
14. [Exemples de création d'alerte dans Telegram (/add et /addurl)](#14-exemples-de-création-dalerte-dans-telegram-add-et-addurl)
15. [Fonctionnement du catalogue et cache de prix BD](#15-fonctionnement-du-catalogue-et-cache-de-prix-bd)
16. [Déploiement en production](#16-déploiement-en-production)
17. [Diagnostic des erreurs courantes](#17-diagnostic-des-erreurs-courantes)
18. [Politique de sécurité et gestion des secrets](#18-politique-de-sécurité-et-gestion-des-secrets)

---

## 1. Présentation de Radar Bot

Radar Bot agit comme un assistant personnel d'achat d'occasion. Conçu pour fonctionner 24 h/24 en production, il reçoit des annonces via un endpoint d'ingestion sécurisé, applique des critères de filtrage stricts définis par l'utilisateur (mots-clés requis, mots-clés exclus, variantes, états acceptés, prix plafond), calcule le prix total estimé (prix de l'article + protection acheteur + frais de port) et délivre instantanément des notifications Telegram ergonomiques.

Pour les bandes dessinées et comics, le bot compare en temps réel le coût de l'annonce avec le prix le plus bas du marché d'occasion livrée (eBay Browse API, Momox, Recyclivre, Gibert) grâce à un catalogue interne et un cache asynchrone rafraîchi quotidiennement.

---

## 2. Fonctionnalités de la V1

- **Accès privé et sécurisé** : Middleware Telegram restreignant l'accès aux seuls identifiants configurés dans `TELEGRAM_ALLOWED_USER_IDS`.
- **Gestion conversationnelle des alertes (FSM)** :
  - `/add` : Assistant interactif pas-à-pas avec claviers inline.
  - `/addurl` : Création instantanée à partir d'un lien de recherche Vinted (analyse syntaxique de l'URL sans requête externe).
  - `/show` : Liste paginée des alertes avec statut (active / en pause) et filtres.
  - `/edit` : Modification granulaire (nom, mots-clés, prix max, états).
  - `/pause` et `/resume` : Suspension et réactivation rapide d'alertes.
  - `/delete` : Suppression avec confirmation explicite.
- **Normalisation Unicode avancée** : Filtrage insensible aux accents (décomposition NFKD), apostrophes typographiques, tirets et séparateurs.
- **Calculateur de coût total transparent** : Prise en compte du prix article, de la protection acheteur (ex: Vinted 0.70 € + 5%) et des frais de port si connus.
- **Notifications Telegram compactes et riches** :
  - Photo principale de l'article.
  - Détail du prix : article seul et total estimé.
  - Évaluation de l'opportunité pour les BD (badge comparateur).
  - Bouton interactif `Voir sur Vinted` (ouverture directe dans le navigateur de l'utilisateur).
  - Bouton interactif `Photos (N)` pour consulter l'album complet.
  - Bouton interactif `Mettre en pause` l'alerte correspondante en 1 clic.
  - Bouton interactif `Détails du marché` (BD uniquement).
- **Statistiques d'activité** : Commande `/stats` affichant l'activité sur 7 ou 30 jours (alertes actives, annonces reçues, matches, top alertes, comparateur BD).

---

## 3. Limites de conformité et éthique

> [!IMPORTANT]
> **Conformité stricte : Zéro automatisation sur les marketplaces.**
> 
> Radar Bot est un outil passif d'aide à la décision :
> - **AUCUNE action automatisée sur Vinted ou toute autre marketplace** : pas d'achat automatisé (sniper), pas d'ajout au panier, pas de paiement, pas de mise en favori, pas d'envoi de messages aux vendeurs.
> - **AUCUN scraping, crawling, reverse engineering ou contournement de protections** (pas de Selenium, Playwright, cookies de session ou headers usurpés).
> - **Ingestion passive** : Le bot reçoit les données de manière standardisée via son API REST sécurisée (`POST /api/v1/ingest/listings`).
> - Toutes les décisions et actions finales d'achat restent **100 % manuelles** et sont opérées par l'utilisateur dans son navigateur officiel via le lien fourni.

---

## 4. Prérequis techniques

- **Python** : 3.12 ou supérieur
- **Docker & Docker Compose** (recommandé pour la production)
- **PostgreSQL** : Version 16+
- **Redis** : Version 7+ (pour le stockage FSM aiogram et le cache distribué)
- **Compte Telegram** actif
- **Clés API eBay Developer** (facultatif mais recommandé pour le comparateur BD)

---

## 5. Création du bot avec BotFather

1. Ouvrez Telegram et cherchez le compte officiel [@BotFather](https://t.me/BotFather).
2. Tapez `/newbot` et suivez les instructions :
   - Choisissez un nom convivial (ex: `Mon Radar Bot`).
   - Choisissez un identifiant se terminant obligatoirement par `bot` (ex: `mon_radar_perso_bot`).
3. BotFather vous remettra votre jeton d'accès d'API HTTP (`TELEGRAM_BOT_TOKEN`), sous la forme :
   `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`
4. Configurez les descriptions et commandes recommandées en tapant `/setcommands` :
   ```text
   add - Créer une alerte pas-à-pas
   addurl - Créer une alerte depuis un lien de recherche
   show - Afficher et gérer les alertes
   edit - Modifier une alerte existante
   pause - Mettre en pause une alerte
   resume - Réactiver une alerte
   delete - Supprimer une alerte
   categories - Afficher les catégories prises en charge
   stats - Consulter les statistiques d'activité
   help - Afficher l'aide
   ```

---

## 6. Comment obtenir son identifiant Telegram

Radar Bot est strictement privé : toute personne non listée reçoit un message de rejet `⛔ Ce bot est privé`.

Pour obtenir votre identifiant numérique unique Telegram :
1. Dans Telegram, contactez le bot officiel [@userinfobot](https://t.me/userinfobot).
2. Il vous répond instantanément avec votre `Id` numérique (ex: `123456789`).
3. Copiez cet identifiant et renseignez-le dans la variable `TELEGRAM_ALLOWED_USER_IDS` de votre fichier `.env` (si plusieurs utilisateurs, séparez-les par des virgules : `123456789,987654321`).

---

## 7. Création des clés eBay Developer

Pour alimenter les comparaisons de prix du marché de la bande dessinée via l'API officielle eBay Browse :
1. Créez un compte sur le portail [eBay Developers Program](https://developer.ebay.com/).
2. Accédez à votre tableau de bord et générez un jeu de clés d'application (**Application Keys / Keyset**) en environnement de **Production**.
3. Récupérez :
   - Votre **Client ID (App ID)** -> `EBAY_CLIENT_ID`
   - Votre **Client Secret (Cert ID)** -> `EBAY_CLIENT_SECRET`
4. `EBAY_ENV` vaut `PRODUCTION` ou `SANDBOX`. Le nom `EBAY_ENVIRONMENT` n'est pas lu.
5. Le bot gère automatiquement le cycle de vie du token OAuth2 `client_credentials` avec renouvellement automatique à expiration.

---

## 8. Installation locale

### Cloner le dépôt
```bash
git clone https://github.com/votre-compte/radar-bot.git
cd "radar-bot"
```

### Initialiser l'environnement virtuel Python
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

### Lancer les services locaux (Postgres et Redis)
Si vous disposez de Docker, vous pouvez lancer uniquement la base et le cache :
```bash
docker compose up -d postgres redis
```

---

## 9. Configuration des variables d'environnement (.env)

Copiez le modèle d'environnement :
```bash
cp .env.example .env
```

La liste complète des clés lues par l'application est dans [`.env.example`](.env.example). Elle correspond à `app/core/config.py`.

- Le niveau de log se règle avec `LOG_LEVEL`.
- eBay se configure avec `EBAY_ENV` (`PRODUCTION` ou `SANDBOX`).
- L'URL du webhook se déclare auprès de Telegram. Elle se termine par `/api/v1/telegram/webhook/` suivi de `TELEGRAM_WEBHOOK_SECRET`.
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` et `DOMAIN_NAME` servent à Docker Compose.

---

## 10. Lancement avec Docker Compose

Pour déployer la stack complète (API FastAPI + scheduler, PostgreSQL 16 et Redis 7) :

```bash
# Construire et démarrer en arrière-plan
docker compose up --build -d

# Vérifier l'état des conteneurs
docker compose ps

# Consulter les logs de l'application (API, scheduler et poller)
docker compose logs -f app
```

L'application écoute par défaut sur le port `8000`.

---

## 11. Gestion des migrations Alembic

Les migrations de schéma de base de données sont versionnées avec Alembic.

### Appliquer les migrations
```bash
# Hors conteneur (dans le virtualenv)
alembic upgrade head

# Ou via Docker Compose
docker compose exec app alembic upgrade head
```

### Créer une nouvelle révision de migration
```bash
alembic revision --autogenerate -m "description_des_changements"
```

---

## 12. Définition et activation du Webhook Telegram

En production, Radar Bot fonctionne exclusivement en mode Webhook HTTPS (aucun polling permanent consommateur de ressources).

### Configuration automatique du Webhook
Vous pouvez associer le webhook directement auprès des serveurs Telegram avec curl :

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://radar.mondomaine.fr/api/v1/telegram/webhook/un_secret_aleatoire_tres_robuste_et_long_32_chars",
    "secret_token": "un_secret_aleatoire_tres_robuste_et_long_32_chars",
    "allowed_updates": ["message", "callback_query"]
  }'
```

Pour vérifier le bon enregistrement du Webhook :
```bash
curl -s "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo" | jq
```

---

## 13. Exemple curl d'ingestion d'annonce

L'ingestion se fait via `POST /api/v1/ingest/listings` avec authentification Bearer token :

```bash
curl -X POST "http://localhost:8000/api/v1/ingest/listings" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer token_d_ingestion_ultra_securise_a_fournir_au_script_fournisseur" \
  -d '{
    "source_name": "vinted",
    "external_id": "4589201938",
    "url": "https://www.vinted.fr/items/4589201938-blacksad-tome-1-quelque-part-entre-les-ombres",
    "title": "Blacksad Tome 1 Quelque part entre les ombres EO",
    "description": "Très bon état général, édition originale grand format cartonné.",
    "category_slug": "bd-comics",
    "item_price_cents": 850,
    "estimated_protection_cents": 113,
    "estimated_shipping_cents": 299,
    "condition": "very_good",
    "seller_name": "bd_collectionneur",
    "seller_rating": 4.95,
    "photos": [
      "https://images1.vinted.net/t/01_023a1_photo1.jpeg",
      "https://images1.vinted.net/t/01_023a1_photo2.jpeg"
    ],
    "isbn13": "9782205049657",
    "published_at": "2026-09-09T18:00:00Z"
  }'
```

### Réponse attendue (200 OK)
```json
{
  "status": "processed",
  "matched_alerts_count": 1,
  "notifications_sent": 1,
  "listing_id": "8fa538e1-5120-4e50-9bb4-bfe24e754a91",
  "created": true
}
```

Si l'annonce est ré-ingérée ultérieurement, la déduplication native l'ignore avec `"status": "ignored_duplicate"`.

---

## 14. Exemples de création d'alerte dans Telegram (/add et /addurl)

### Méthode 1 : `/add` (Assistant pas-à-pas)
1. Envoyez `/add` au bot.
2. Cliquez sur la catégorie souhaitée : `📚 BD & Comics`, `👕 Vêtements` ou `💻 Tech`.
3. Saisissez le nom de votre alerte (ex: `Blacksad EO`).
4. Indiquez les mots-clés obligatoires séparés par des virgules (ex: `blacksad, tome 1`).
5. (Optionnel) Indiquez les variantes séparées par des virgules (ex: `integrale, edition collector`).
6. (Optionnel) Indiquez les mots-clés exclus (ex: `abime, incomplet, copie`).
7. Définissez votre prix plafond en euros (ex: `15` ou `15.50`).
8. Sélectionnez les états acceptés à l'aide des boutons à bascule (ex: Neuf avec étiquette, Très bon état).
9. Validez le récapitulatif pour activer l'alerte !

### Méthode 2 : `/addurl` (Depuis une recherche Vinted)
1. Effectuez votre recherche sur le site ou l'application Vinted avec vos filtres (prix, état, etc.).
2. Copiez l'URL de la recherche :
   `https://www.vinted.fr/catalog?search_text=tintin+au+tibet&price_to=12`
3. Envoyez la commande au bot :
   `/addurl https://www.vinted.fr/catalog?search_text=tintin+au+tibet&price_to=12`
4. Le bot extrait les paramètres de l'URL côté serveur (sans aucune requête réseau vers Vinted) et pré-remplit instantanément l'alerte.

---

## 15. Fonctionnement du catalogue et cache de prix BD

Pour les bandes dessinées, mangas et comics, le bot propose un avantage décisionnel majeur :

```
[ Ingestion annonce BD ]
           │
           ▼
[ Identification Catalogue ] ────► ISBN-13 / ISBN-10 / Titre normalisé
           │
           ▼
[ Consultation Cache Marché ] ───► (Zéro requête réseau externe au moment de l'alerte)
           │
           ├─► Trouvé & valide (< 24h) : Affichage immédiat du badge & différence
           │
           └─► Absent ou expiré : Alerte envoyée sans prix de marché,
                                 Livre inscrit pour le prochain cycle de rafraîchissement
```

### Rafraîchissement automatique (`app/tasks/refresh_book_prices.py`)
- Planifié quotidiennement à **07:00** et **19:00** (Europe/Paris).
- Interroge les connecteurs agréés via un `ProviderRegistry` avec un sémaphore de concurrence (`asyncio.Semaphore(5)`) pour ménager les APIs :
  - **eBay Browse API** : API officielle OAuth2.
  - **Momox** : Barème d'occasion livrée (port offert dès 19 € d'achats).
  - **Recyclivre** : Frais de port standards inclus.
  - **Gibert** : Articles d'occasion uniquement (port 3.90 € ou offert dès 30 €).
- Enregistre un instantané d'audit (`market_price_snapshots`) et met à jour le cache chaud (`book_market_cache`).

---

## 16. Déploiement en production

### Architecture recommandée
- **Reverse Proxy** : NGINX ou Traefik gérant les certificats SSL/TLS Let's Encrypt.
- **Serveur d'application unifié** : Conteneur `radar_app` (FastAPI + Uvicorn, bot Telegram, APScheduler et Vinted Poller intégrés) exposé uniquement au reverse proxy.
- **Base de données** : PostgreSQL 16 avec sauvegardes régulières (`pg_dump`).
- **Cache / FSM** : Redis 7 avec persistance AOF activée.

### Commandes Makefile utiles
```bash
make up         # Démarre tous les services Docker
make down       # Arrête les conteneurs
make logs       # Affiche les logs en continu
make migrate    # Applique les migrations Alembic
make test       # Exécute la suite complète de 81 tests pytest
make lint       # Vérifie la conformité ruff et mypy
make format     # Formate automatiquement le code avec ruff
```

---

## 17. Diagnostic des erreurs courantes

### 1. `TokenValidationError` au démarrage du bot
- **Cause** : La variable `TELEGRAM_BOT_TOKEN` est absente, mal copiée ou contient des guillemets/espaces superflus.
- **Solution** : Vérifiez que le format correspond exactement à `<chiffres>:<caractères>` (ex: `123456789:ABC...`).

### 2. Le bot ne répond pas sur Telegram
- **Vérifications** :
  - L'URL de votre Webhook est-elle bien en **HTTPS** valide avec certificat officiel (Let's Encrypt) ?
  - Le `TELEGRAM_WEBHOOK_SECRET` passé dans l'URL correspond-il à celui dans `.env` ?
  - Votre identifiant Telegram est-il bien renseigné dans `TELEGRAM_ALLOWED_USER_IDS` ?
  - Consultez les logs : `docker compose logs -f app`

### 3. Erreur 401 sur l'ingestion (`/api/v1/ingest/listings`)
- **Cause** : Le header `Authorization: Bearer <INGEST_API_TOKEN>` est manquant ou ne correspond pas au secret configuré.
- **Solution** : Comparez la valeur de `INGEST_API_TOKEN` dans votre script d'ingestion et dans `.env`.

### 4. Absence de prix de marché sur les alertes BD
- **Causes normales** :
  - L'annonce ne contient pas d'ISBN et le titre n'a pas pu être associé avec certitude.
  - Le livre n'a pas encore été analysé lors du cycle quotidien de 07:00 ou 19:00.
  - Aucune offre d'occasion en stock n'a été trouvée auprès des fournisseurs pour cet ISBN.
- **Vérification** : Vous pouvez déclencher manuellement un rafraîchissement immédiat via l'endpoint interne :
  `POST /api/v1/internal/tasks/refresh-prices` avec le header `Authorization: Bearer <INTERNAL_API_TOKEN>`.

---

## 18. Politique de sécurité et gestion des secrets

- **Principe du moindre privilège** : L'image Docker s'exécute avec un utilisateur système non privilégié (`appuser:1001`).
- **Secrets protégés** : Toutes les variables sensibles (`TELEGRAM_BOT_TOKEN`, `INGEST_API_TOKEN`, `INTERNAL_API_TOKEN`, etc.) sont encapsulées via le type `SecretStr` de Pydantic V2 pour empêcher toute fuite accidentelle dans les logs ou les traces d'erreurs.
- **Isolation réseau** : La base PostgreSQL et l'instance Redis ne doivent jamais être exposées publiquement sur Internet.
- **Pas de scraping** : En évitant tout scraping direct ou usurpation de sessions sur les marketplaces, Radar Bot préserve votre IP et évite tout bannissement ou violation de conditions d'utilisation.

---

*Développé avec rigueur et passion pour des achats d'occasion malins et éclairés.* 🚀
