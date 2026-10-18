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
