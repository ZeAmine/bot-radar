# Déployer et reprendre une interruption

Statut : procédure actuelle ; à lire obligatoirement avant un déploiement autorisé.
Périmètre : livraison du code, schéma cible et contrôles de reprise. Cette page
n'autorise ni accès production ni modification de passerelle.

Cible documentée : `ubuntu@88.96.56.44`, clé `~/.ssh/ssh-key-2026-09-10.key`,
répertoire `~/bot-radar/`, domaine `https://radar.aminezegmou.fr`.
La cible est un arbre rsync, **pas un dépôt Git** : un push ne déploie rien.
Le gateway Caddy partagé vit hors dépôt dans `~/gateway/`, sur `web_gateway`.
Ne pas réintroduire un Caddy local ni modifier les autres applications du gateway.

## Préconditions et commande canonique

Les contrôles complets et la revue requise dans [AGENTS](../../AGENTS.md) doivent
être satisfaits. Relire [deploy.py](../../scripts/deploy.py),
[preflight_deploy.py](../../scripts/preflight_deploy.py) et la
[procédure BD](../bd-preparation.md), notamment pour une reprise ou un retour.
Exécuter soi-même les opérations autorisées, sans demander à l'utilisateur de
copier-coller des commandes. Sans permission réseau/clé, signaler le blocage.

```sh
make deploy
```

Cette commande : pré-vol → arrêt et confirmation de l'arrêt de l'ancienne app →
rsync → construction de l'image cible sans app → migration ponctuelle de cette
image via entrypoint Alembic → lecture de révision → démarrage → santé et contrôles
stricts. Aucun scheduler ou poller dans le conteneur de migration. Arrêt au premier
échec ; pas de lancement de l'étape suivante. `DEPLOY_ARGS=--dry-run` affiche l'ordre.
`make preflight` et `make verify-deploy` restent disponibles pour les étapes distinctes.
Ne pas recopier une autre séquence de commandes à côté de celle du script.

Le rsync exclut `.git` et `.env` : il préserve la configuration distante et ne
transfère pas le secret local. Contrôler `APP_ENV=production`, code synchronisé,
conteneur, schéma et scheduler. `.env.example` ne doit jamais écraser un `.env` réel.

## Échec et retour

L'interruption couvre synchronisation, construction, migration et démarrage jusqu'à
la santé réelle, sans durée garantie. Un échec après arrêt peut laisser le service
arrêté et l'arbre partiellement synchronisé ; ne pas redémarrer automatiquement
l'ancien code sur cet arbre. Après démarrage, la cible peut être active malgré un
contrôle échoué. Constater conteneur, code et schéma avant de choisir la reprise.
Aucune restauration automatique, aucun downgrade destructeur ou version falsifiée.

Les anciennes préparations BD exigent une maintenance bloquant les updates Telegram
pour un retour vers un code qui ignore les intentions. Inhiber le scheduler seul
ne protège pas `/resume`. La procédure BD détaille compatibilité et artefact de retour.
La sauvegarde/restauration exige en plus [son propre contrat](../backup-recovery.md).

## Portée des preuves

`/health` atteste la vivacité HTTP. Sa sonde IA ne prouve pas une génération ; SQL,
Redis, scheduler et poller ne sont pas contrôlés par cette route. Le script exige
ses autres contrôles ; aucun ne prouve une collecte Vinted ou réception Telegram réelle.
Les recettes historiques `delivery-0010` ne sont pas la procédure courante, et une
preuve locale ne démontre jamais la révision ou la configuration d'une production réelle.
