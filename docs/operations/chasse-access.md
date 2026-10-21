# Accès authentifié Chasse aux livres

Statut : procédure locale du collecteur corrigé ; ne vaut pas vérification de production.

## Configuration confidentielle

Les seules variables nécessaires sont `CHASSE_AUX_LIVRES_DUIH` pour amorcer
l'accès historique, puis `CHASSE_ACCOUNT_EMAIL` et `CHASSE_ACCOUNT_PASSWORD`
pour la reconnexion normale du compte dédié. Leurs valeurs restent dans le
`.env` privé de l'environnement cible. `.env.example` conserve des valeurs vides.
Les guillemets usuels du format dotenv sont acceptés. Ne pas passer les valeurs
sur la ligne de commande, les copier dans un ticket ou exporter tous les cookies.

Le collecteur conserve dans Redis un jeton validé, avec sa provenance, la date de
vérification et l'état de reprise. Le jeton renouvelé peut remplacer l'amorce
périmée du `.env`. Changer explicitement l'amorce ou les identifiants invalide
cet état : un ancien enregistrement Redis ne masque pas cette nouvelle configuration.
La clé historique `radar:chasse:duih` n'est un repli qu'en l'absence d'amorce configurée.
L'injection interne explicite actualise aussi l'état de session utilisé ; elle
n'atteste pas à elle seule un accès authentifié.

## Régulation des appels ordinaires

`app/providers/cal_governor.py` arbitre les requêtes ordinaires émises par le
fournisseur CAL : recherche, fiche, lookup initial, sondages et reprises. Chaque
requête HTTP réellement émise consomme un jeton de collecte, un quota de fenêtre
et un espacement.

Cinq plafonds distincts, chacun bornant une chose différente. Aucun n'est un
seuil garanti par CAL : ce sont des valeurs prudentes choisies localement, pas
la mesure de ce que le site tolère.

| Notion | Paramètre | Valeur initiale | Ce qu'elle borne |
| --- | --- | --- | --- |
| Livres admis | `CAL_MAX_BOOKS_PER_PASS` | 2 | collectes CAL ouvertes par passe planifiée |
| Requêtes émises | `CAL_MAX_REQUESTS_PER_COLLECTION` | 12 | requêtes HTTP d'une seule collecte, sondages et reprises compris |
| Concurrence | `CAL_MAX_CONCURRENT` | 1 | requêtes CAL simultanées, tous processus confondus |
| Espacement | `CAL_MIN_INTERVAL_SECONDS` | 2 s | intervalle entre deux ouvertures de collecte |
| Fenêtre de quota | `CAL_MAX_REQUESTS_PER_WINDOW` / `CAL_WINDOW_SECONDS` | 30 par 60 s | requêtes émises sur une fenêtre glissante, toutes collectes confondues |
| Fenêtre d'admission | `CAL_PASS_WINDOW_SECONDS` | 900 s | durée au-delà de laquelle un plafond d'admission périmé ne bloque plus |
| Bail de concurrence | `CAL_LEASE_TTL_SECONDS` | 45 s | durée de vie d'une autorisation dont le porteur a disparu |

Repères arithmétiques, sans valeur de sécurité : un espacement de deux secondes
autorise au plus 30 ouvertures par minute, soit 1800 par heure ; c'est la fenêtre
de quota (30 par minute) qui contraint réellement le débit, pas l'espacement.
Le plafond de livres admis est le seul qui limite le nombre de livres CAL traités
par une passe ; il ne réduit ni le lot global de huit livres, ni le débit des
autres fournisseurs. Ce plafond est **global sur une fenêtre glissante de 900 s**,
et non par identifiant de passe : on doit le lire « deux admissions de livre par
fenêtre glissante ». Un identifiant de passe ne compte pas ; changer de passe ne
recharge donc pas le quota.

Seule une collecte de livre consomme une place. L'entretien de session
(`maintain()`) et les scripts (`chasse_prices.py`, `diagnose_chasse_access.py`)
sont admis en `counts_as_book=False` et restent bornés par la concurrence et la
fenêtre de requêtes, jamais au détriment des livres de la passe.

L'espacement ne s'applique qu'entre **collectes distinctes**, c'est-à-dire à la
première requête d'une collecte : il ne s'intercale jamais entre les sondages
d'une même collecte, dont la fenêtre totale est de 1,5 s. Le quota de requêtes
et la concurrence continuent de s'appliquer à chaque requête. Il ne s'intercale
pas entre le lookup et ses sondages, dont la fenêtre bornée reste de 1,5 s.

Coût d'une collecte dans l'ordre réel du code : trois tentatives de recherche à
deux requêtes (6), une fiche (1), un lookup (1), trois sondages (3), une recherche
de repli par titre dans la même collecte (2). Le plafond de 12 requêtes par
collecte borne cet ensemble ; avec deux livres admis par passe, la borne par
passe est de 24 requêtes, elle-même plafonnée par la fenêtre de quota.

Une restriction ferme l'arbitre : `bot_challenge` ouvre un repos de six heures,
`429` respecte `Retry-After` et prend sinon quinze minutes. Une collecte refusée
porte le statut `skipped` avec la raison du report et l'échéance ; elle ne compte
pas comme échec fournisseur et ne renouvelle pas la session. Les requêtes déjà en
vol ne sont pas annulables : leur nombre est borné par `CAL_MAX_CONCURRENT`.

Un refus prononcé **avant toute requête HTTP** — quota d'admission, fenêtre de
concurrence, circuit ouvert, essai en cours, stockage indisponible — est un
**report administratif**, reconnu à la raison du régulateur
(`ADMINISTRATIVE_DEFERRAL_REASONS`) — et non au seul statut `skipped`, que
prennent aussi les composantes non demandées — **et** à une preuve d'émission
nulle : le budget de collecte compte les requêtes réellement parties, ce compte
traverse l'agrégation des fournisseurs jusqu'au résultat que lit la persistance.
Un refus survenu après le départ d'une requête n'est donc pas un report
administratif, c'est une interruption de collecte. Ce report :

- n'incrémente aucun échec de composante et ne fait donc pas escalader le recul ;
- n'enregistre aucune tentative réseau : `last_attempt_at` reste inchangé ;
- n'avance ni ne retire les dates de réussite, ni les offres confirmées ;
- fixe `next_refresh_at` à une reprise bornée : au moins une demi-passe pour
  `pass_budget` et `probe_in_flight`, cinq minutes pour un stockage
  indisponible, l'échéance du circuit pour un circuit ouvert ;
- laisse une trace dans `refresh_state["administrative"]` (nombre, première et
  dernière fois, raisons, échéance) et une ligne de journal par livre, pour
  qu'un livre reporté plusieurs fois soit repérable.

Une collecte **commencée puis interrompue** — au moins une tentative non `skipped`
— garde le comportement normal : échec compté, `partial` possible, recul de
six heures. La distinction tient à l'émission : si aucune requête n'est partie,
aucun échec n'est imputé au livre.

Un refus administratif n'est ni une panne ni une donnée périmée : le bilan de
passe compte les tentatives non effectuées séparément (`market_health` ignore
les `skipped` dans les incidents), et aucune notification d'incident n'est
déclenchée par ce seul motif.

Effet sur la fraîcheur, mesuré sur `persist_market_result` :

| Situation | Statut écrit | `last_attempt_at` | Recul |
| --- | --- | --- | --- |
| CAL seul, refusé avant émission | `deferred` | inchangé | aucun ; reprise bornée |
| Collecte commencée puis interrompue | `partial` ou `failed` | renseigné | 6 h → 1 j → 7 j, plafonné |
| CAL refusé, une autre source répond | `empty` | renseigné | aucun |

Seule une collecte réellement commencée produit un échec : elle escalade
`RETRY_DELAYS` — 6 h, puis 1 jour, puis 7 jours — sans dépasser trois échelons.
Un livre servi revient à `REFRESH_INTERVAL` (7 jours) ; un livre reporté reste dû
dès sa reprise, donc avant un livre servi.

`market_refresh_policy.component_due` n'a pas été modifiée : son recul de six
heures ne s'applique qu'aux statuts `failed`, `partial` et `skipped`. Le statut
`deferred` n'y figure pas, et le chemin administratif ne renseigne pas
`last_attempt_at` : aucun recul d'échec n'est imputé à un livre reporté. La
reprise vient de `next_refresh_at`, fixé par le report lui-même.


## Circuit et reprise progressive

Le circuit CAL suit trois états, décidés par `cal_governor.phase()` :

| État | Condition | Effet sur les collectes |
| --- | --- | --- |
| `closed` | repos expiré et aucun essai en cours | admission normale, dans le plafond de la passe |
| `open` | `open_until` non atteint | tout appel est reporté, y compris ceux déjà en attente |
| `probing` | repos expiré, un essai admis et non encore conclu | une seule collecte d'essai, toutes passes confondues |

Transitions :

- un défi ouvre le circuit six heures ; un `429` ouvre selon `Retry-After`,
  sinon quinze minutes ;
- à l'expiration, la première collecte admise devient l'essai unique ;
- un essai réussi rend le circuit `closed` ; l'essai **consomme une place du
  plafond d'admission**, comme n'importe quelle collecte de livre, donc il ne donne
  jamais d'admission supplémentaire ;
- aucune clôture, réussie **ou échouée**, ne peut effacer ni raccourcir une
  restriction active : le script compare l'échéance proposée à la restriction en
  cours et la conserve si elle est postérieure à l'ouverture de l'essai et plus
  longue. La branche d'échec ne remplace donc jamais un défi de six heures par
  un repos de trente minutes, ni un `Retry-After` long par le repos par défaut ;
- chaque essai porte un **jeton unique** ; une conclusion ne vaut que pour le
  jeton de l'essai qu'elle clôt. Une collecte admise avant une restriction, ou une
  collecte plus ancienne, ne peut pas conclure l'essai d'une autre.
- un essai partiel, invalide ou interrompu n'y rend jamais l'admission pleine :
  un défi pendant l'essai rouvre six heures, un autre échec trente minutes ;
- la restriction d'une réponse est enregistrée **avant** que le bail ne soit
  rendu disponible : un appel concurrent attend le bail, puis voit la
  restriction. Le bail est libéré dans tous les cas, y compris sur erreur et sur
  annulation ;
- une collecte dont la composante `current` est `skipped` n'est **pas** un succès :
  un report du régulateur, une restriction, un budget épuisé ou un stockage
  indisponible ne ferment jamais le circuit. Seule une collecte réellement
  exécutée — toutes ses tentatives `current` en `success` — vaut clôture réussie ;
- l'essai est borné par un délai de validité de soixante secondes. Son expiration
  ou son abandon **ne vaut jamais rétablissement** : la restriction reste
  enregistrée et le circuit reste `open`. Un seul nouvel essai est alors admis ;
  toute autre admission simultanée est refusée en `probe_in_flight`. Aucune
  admission normale n'a lieu avant une conclusion de succès valide, et le jeton
  de l'essai abandonné ne peut pas conclure le nouvel essai. La prise d'essai est
  atomique : un seul appel obtient l'état `probing` ;
- l'essai est exclusif au moment de l'émission, pas seulement à l'admission :
  tant que le circuit exige un essai (`open_until > 0`), `ACQUIRE_SCRIPT`
  vérifie, dans la même opération atomique, que le jeton porté par le budget de
  collecte est bien celui de l'essai actif et non expiré. À défaut, la requête
  est refusée en `probe_in_flight` **sans qu'aucune requête ne parte, première
  requête comprise**. L'exclusivité ne dépend donc pas du nombre de requêtes
  déjà émises : une collecte admise avant la restriction, une requête tardive
  d'un essai expiré, une collecte reprise sans essai attribué ou un ancien
  budget sont tous refusés. Un porteur valide émet dans ses budgets habituels,
  et une nouvelle restriction l'arrête comme les autres. Quand le circuit est
  normalement fermé (`open_until == 0`), une collecte ordinaire émet sans jeton.
  Une ancienne admission n'est jamais convertie en admission d'essai ;
- une collecte déjà admise **ne reprend pas** après rétablissement : elle est
  réadmise. Le fournisseur refuse en `readmission_required` un client démonté
  dont le budget existe encore, au lieu de continuer sur l'admission
  précédente ; seule une collecte fraîche, avec sa propre admission, émet.

Une collecte complète sans offre éligible compte comme succès technique : c'est
le statut des tentatives `current` qui décide, pas le nombre d'offres.

Les paliers de montée en charge ne sont pas automatiques : le plafond de deux
livres admis par passe reste la valeur initiale jusqu'à une décision explicite
fondée sur l'observation.

## Chemins couverts et limites restantes

Raccordés au régulateur, en `GET` comme en `POST` : la recherche, la fiche, le
lookup initial, les sondages, l'historique, l'entretien de session
(`maintain()`), la connexion automatique (`_login`, y compris sa page de
protection, son envoi de formulaire et sa page d'accueil), ainsi que les scripts
`chasse_prices.py` et `diagnose_chasse_access.py`.

La connexion automatique déclenchée par `get_history()` ou `maintain()` reçoit le
même client régulé que le reste de la collecte : ses trois requêtes sont comptées
comme les autres, un circuit ouvert les interdit aussi, et chacune s'arrête si une
restriction apparaît — aucune étape suivante n'est tentée. Le formulaire, les
identifiants et la logique de connexion ne changent pas : seul le transport des
requêtes est arbitré.

Restent hors du budget de requêtes :

- `refresh_chasse_token.py`, qui s'exécute hors application avec son propre client
  synchrone et n'est pas raccordé ; il relève d'une exécution manuelle ;
- l'injection de session vers l'API interne, qui n'est pas un appel CAL.

Ces chemins restent soumis au repos partagé de la session, pas au budget de
requêtes.

## Vérification locale contre un Redis de test

Les décisions atomiques ne se prouvent pas avec un double mémoire : les tests qui
les couvrent exigent une instance Redis jetable, fournie par
`CAL_TEST_REDIS_URL`. Sans cette variable, ils sont **ignorés** — jamais
remplacés par un double, jamais comptés comme verts.

```sh
docker run -d --rm --name radar-redis-test -p 127.0.0.1:6390:6379 \
  redis:7-alpine redis-server --save '' --appendonly no
CAL_TEST_REDIS_URL=redis://127.0.0.1:6390/0 \
  /chemin/vers/.venv/bin/python -m pytest tests/unit/test_cal_governor_redis.py -q
docker stop radar-redis-test
```

L'instance écoute sur la boucle locale d'un port haut, sans persistance ni
exposition publique. Elle ne doit jamais pointer vers un Redis de production :
la variable est lue dans le seul environnement de test.

Ces tests exercent plusieurs instances du régulateur sur les mêmes clés, en
parallèle : admission simultanée, concurrence maximale, quotas, essai unique après
repos, restriction concurrente avec la fin d'une collecte, abandon et expiration,
stockage indisponible.

## Vérification locale sans base ni Telegram

Depuis le checkout contenant le correctif, avec son interpréteur Python :

```sh
python scripts/diagnose_chasse_access.py \
  --env-file /chemin/prive/.env \
  --session-file /chemin/prive/chasse-session.json
```

Le parent du fichier de session doit exister. Ce fichier privé, écrit atomiquement
avec des droits `0600`, contient un secret : le conserver hors dépôt et ne pas
l'envoyer. Le diagnostic ne charge que les trois variables Chasse aux livres,
neutralise les autres variables avant les imports applicatifs et limite les
requêtes au domaine HTTPS de la source. Il n'utilise ni Redis, ni DB, ni Telegram.
Il ne montre aucun corps de réponse, cookie, mot de passe ou URL d'historique signée.

Relancer la même commande dans un second processus vérifie la réutilisation du
jeton persisté. `account_login_attempted: false` avec `history_access_verified: true`
atteste cette réutilisation locale ; cela ne garantit pas une durée de validité
illimitée côté site. Une absence d'offres historiques ne vaut pas erreur de connexion
si la réponse authentifiée est structurée et valide.

La sonde porte sur Jean Doux, ISBN 9782756079332, avec 365 jours, « Très bon état + »
et livraison incluse. Le JavaScript public du site utilise `y` pour le graphique ;
`ws=true` signifie sans livraison et est donc omis ici. La date `x` est exprimée
en millisecondes Unix et peut être une chaîne. L'infobulle `t` n'est pas la valeur
numérique de référence. Le montant observé est un résultat, jamais une constante
attendue du collecteur.

## Cycle de vie et intervention humaine

Une réponse historique structurée valide l'accès ; un HTTP 200 vide ou une page
HTML ne le valide pas. Une réponse JSON de schéma inconnu reste une erreur de
réponse, sans prétendre que le compte est expiré. Les erreurs serveur et les
limitations de débit ne déclenchent pas de connexion au compte.

Si nécessaire, un seul travail de reconnexion par processus utilise le formulaire
normal avec ses champs anti-rejeu, extrait un nouveau jeton, puis vérifie l'accès
historique avant de conserver ce jeton. Les redirections de connexion ne sont pas
suivies avec les identifiants. Aucun CAPTCHA ni second facteur n'est résolu.
La collecte reprend au maximum une fois avec le nouveau jeton.

Les passes existantes du scheduler vérifient l'accès au plus toutes les six heures,
indépendamment de l'âge des prix, en réutilisant l'URL d'un livre déjà consulté.
Cette vérification ne collecte pas d'offres et ne change aucune date de prix.
Avant la première collecte, l'accès sera vérifié sur le premier ouvrage demandé.
Une panne d'accès impose six heures de repos partagé. Si la reconnexion échoue,
une notification administrative demande de reconnecter le compte ; son acquittement
est conservé et évite de répéter le message tant que l'incident persiste.
Les prix restent gérés par le service de persistance existant.

Redis indisponible laisse un repli mémoire et un diagnostic interne ; la persistance
après redémarrage n'est alors pas garantie. Le dédoublonnage de remise administrative
ne garantit pas une livraison exactement une fois si Telegram accepte le message
sans que son acquittement puisse être conservé.

La maintenance est portée par l'unique processus applicatif. Lors d'un futur
déploiement explicitement autorisé, retirer le cron autonome historique après
vérification de la maintenance intégrée, pour éviter deux autorités de reconnexion.
La concurrence entre un ancien cron et le nouveau runtime n'est pas couverte par
le verrou mémoire du processus. Cette procédure ne désactive aucun cron à elle seule.

## Preuves locales du 18 octobre 2026

La connexion normale au compte dédié a rendu l'historique ; la sonde corrigée a
obtenu 936 centimes, point du 2 juillet 2026 à 22:53 UTC, soit le 3 juillet à 00:53
à Paris, avec zéro centime de livraison. Un second processus a retrouvé le même
résultat sans nouvelle connexion. Le jeton seul a également été vérifié sans
réutilisation des cookies de connexion : aucun export complet de session n'est
nécessaire pour cette collecte observée.

Ces preuves ne portent ni sur une expiration future effective du nouveau jeton,
ni sur le Redis de production, ni sur l'enregistrement et le rendu de ces données
dans les alertes existantes. Les scénarios d'expiration, de concurrence et de panne
sont aussi vérifiés avec des transports factices, séparément des appels réels.

Le diagnostic accepte aussi `--full-collection` pour parcourir recherche, fiche,
offres et historique dans le collecteur réel. Dans ce mode, un échec du collecteur
produit un code de sortie non nul même si la vérification directe de la session
réussit. Les seules données de diagnostic supplémentaires sont les statuts, types
de champs, nombres de marchands et montants ; aucun contenu d'authentification.
Le parcours complet a retrouvé 936 centimes et sept marchands lors de la recette.
Une autre tentative a rencontré une réponse invalide avant les offres ; sa cause
précise n'a pas été établie. Cette intermittence n'est pas classée « aucune offre ».
Les points publics capturés ont aussi été persistés, relus et affichés par les
services BotRadar dans la base isolée ; aucune alerte réelle n'a été modifiée.
