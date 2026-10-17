# Contrôle de silence de la chaîne d'alerte (niveau 1)

Statut : procédure actuelle, **lecture seule**. Ce contrôle ne corrige rien et
n'autorise aucune correction. Il **capture** une autorisation humaine, sans
exécuter quoi que ce soit. Aucun timer n'est activé par ce dépôt.

Périmètre : une stagnation entre l'ingestion d'annonces et les notifications
Telegram. Le script [`scripts/check_alert_chain.py`](../../scripts/check_alert_chain.py)
compare, sur une fenêtre glissante, les annonces reçues, les correspondances
créées, les notifications acquittées et les correspondances encore actionnables
en retard. Un incident critique exige qu'une correspondance attende réellement un
traitement dont le délai admissible est dépassé.

## Ce que le contrôle fait et ne fait pas

- **Fait** : lit `listings` et `listing_matches`, qualifie l'incident et envoie
  une alerte Telegram via `notify_admins`, avec un clavier d'approbation. Écrit
  une trace JSONL et un état durable partagé (incidents, décisions).
- **Ne fait pas** : aucune écriture en base, aucun appel IA, aucune correction,
  aucun agent de correction, aucun worktree automatique, aucune ouverture de PR,
  aucun merge, aucun déploiement, aucune modification de CI, permissions, secrets
  ou workflows.
- **Aucun secret** n'apparaît dans le message ni dans la trace : uniquement des
  identifiants internes (`alert_id`, `match_id`, `listing_id`), des états, des
  compteurs et le SHA du commit. Ni titre, ni URL d'annonce, ni donnée personnelle.

Un relevé sans incident **ne prouve pas** l'absence de bug, **ni que le matching
est sain**, **ni qu'une notification a été reçue par Telegram**. Il signifie
seulement qu'aucune correspondance actionnable échue n'a été observée. Ce contrôle
ne couvre ni la collecte Vinted, ni la réception réelle côté Telegram, ni la
chaîne des prix, ni la vivacité du scheduler, ni l'état de PostgreSQL/Redis.

## Signaux et seuils

| Paramètre | Défaut | Sens |
| --- | --- | --- |
| `--window-hours` | `2` | Fenêtre des annonces, des correspondances et des acquittements |
| `--stuck-hours` | `1` | Âge minimal d'une correspondance actionnable en retard |
| `--max-sample` | `8` | Identifiants listés dans le message |
| `--dedup-seconds` | `3600` | Fenêtre du dédoublonnage Redis |

Déclenchement déterministe :

1. **Critique** — `blocked_actionable_due > 0` : une correspondance attend
   réellement un traitement, son échéance `next_attempt_at` est passée et elle est
   plus ancienne que `--stuck-hours`. Une correspondance fraîche ou une reprise
   prévue dans le futur n'est pas comptée.
2. **Aucun incident** — sinon. `matches_created` et `matches_notified` restent des
   compteurs de contexte : leur seule valeur ne déclenche jamais d'incident.

Des annonces ingérées sans aucune correspondance ne déclenchent donc rien : il
n'y avait rien à notifier. C'est un fait, pas une preuve de bonne santé.

`blocked_actionable_due` compte les correspondances `notified_at IS NULL`, dans un
état que la reprise F03-B doit faire progresser (`ready`, `deferred`, `sending`, ou
`unknown` avec un reçu exploitable), dont `next_attempt_at` est passée et créées
avant `--stuck-hours` (garde contre les correspondances en cours d'envoi).
`notified` et `abandoned` sont terminaux. `non_terminal_due` compte, à titre
informatif, toutes les correspondances non terminales échues ; il ne déclenche
aucun incident à lui seul.

### `unknown` avec ou sans reçu (F03-B)

`unknown` n'autorise **aucun nouvel envoi** : `recover_notifications` s'arrête sur
cet état. Mais un `unknown` porteur d'un reçu exploitable (`telegram_message_id > 0`,
`telegram_chat_id != 0`, `telegram_bot_id > 0`, `attempt_id` non nul) reste
**actionnable pour sa finalisation** : `complete_stored_receipt` exécute le
marquage SQL, sans renvoyer de message. Un tel reçu resté non finalisé au-delà de
la garde d'âge est donc un vrai blocage et compte dans `blocked_actionable_due`.
Un `unknown` **sans** reçu n'est ni renvoyé ni finalisable : il ne compte que dans
`non_terminal_due`, sans incident.

## Exécution manuelle

Le service `app` monte désormais le volume nommé `alert_chain_state` sur
`/var/lib/botradar` : `up`, `exec` et `run` voient donc le **même** répertoire
d'état. Aucune option `-v` n'est plus nécessaire, et il ne faut pas en ajouter :
un montage différent créerait un second état, donc un second `alert_id`.

Depuis le conteneur applicatif (code déjà présent dans l'image) :

```sh
docker compose run --rm --no-deps -T app python scripts/check_alert_chain.py
```

Simulation sans notification ni écriture persistante :

```sh
docker compose exec -T app python scripts/check_alert_chain.py --dry-run
```

Le répertoire d'état est inscriptible par `appuser` (uid `1001`) : l'image crée
`/var/lib/botradar` avec ces droits avant `USER appuser`, et un volume neuf hérite
de cette initialisation. `--no-state` désactive l'état durable ; `--dry-run`
l'affiche sans l'écrire et sans notifier.

Codes de sortie : `0` aucun incident, `1` incident détecté, `2` contrôle impossible
(base ou configuration indisponible).

**Un code `2` n'est pas une absence de symptôme.** Il ne clôt jamais un incident :
au contraire, l'incident ouvert est conservé. Il n'est pas non plus confondu avec
un incident métier : voir « Contrôles impossibles » ci-dessous.

L'image applicative ne contient pas `.git` (le rsync l'exclut) : le commit analysé
vaut donc `inconnu` par défaut dans le conteneur. Pour l'attester, passer
`--commit <sha>` ou `-e GIT_COMMIT=<sha>` — le SHA déployé est affiché par
`make deploy`.

## Trace et statuts

La trace JSONL (`alert-chain.jsonl`) conserve, pour chaque exécution : `alert_id`,
horodatage, seuils, résultats (`ingested`, `acknowledged`, `matches_created`,
`matches_notified`, `blocked_actionable_due`, `non_terminal_due`, échantillon),
statut et commit analysé. Le script n'écrit que trois statuts :

- `ouvert` — incident détecté (nouveau ou toujours reproduit, même `alert_id`) ;
- `vérifié` — un relevé ultérieur ne reproduit plus le symptôme ;
- `ok` — aucun incident, aucune alerte ouverte.

`en cours` et `corrigé` ne sont **pas** décidés par le script : ils décrivent une
action humaine (PR ouverte, correctif livré) et restent portés par la PR. La règle
de clôture est celle du symptôme : **un correctif proposé, fusionné ou déployé
laisse l'alerte ouverte**. Elle n'est `vérifié` que lorsque le même contrôle,
rejoué après livraison, ne la reproduit plus. S'il la reproduit, l'alerte reste
`ouvert` (statut « rouvert » côté humain) et une nouvelle alerte part selon le
dédoublonnage — **aucune correction automatique**.

## État durable partagé

Le répertoire `/var/lib/botradar` fait autorité sur trois questions, et une seule
source répond à chacune :

| Fichier | Autorité | Contenu |
| --- | --- | --- |
| `incidents.jsonl` | État des incidents | Un fait par ouverture et par clôture |
| `decisions.jsonl` | Décisions humaines | Approbation, refus, expiration, suspension, reprise |
| `suspension.json` | État de suspension | Drapeau et dernier changement |
| `technical.json` | Contrôles impossibles | Compteur consécutif et alerte technique déjà émise |
| `dedup/*.json` | Dédoublonnage inter-processus | Créneau de notification réservé |

Les deux journaux sont *append-only* et les écritures sont atomiques
(`tempfile` + `os.replace`), sous verrou `flock`. L'état courant est reconstruit
par relecture complète : une interruption ne peut perdre qu'une écriture non
terminée, jamais réécrire l'histoire. **Aucun fichier dérivé ne fait autorité**,
et il n'en existe pas.

Conséquences vérifiées par les tests : l'`alert_id` survit à `compose run`,
`compose exec`, à un redémarrage et à un déploiement ; deux exécutions séparées
sans Redis ne notifient pas deux fois le même incident.

Le volume survit à `make deploy` : `step_stop` et `step_start` ne le touchent pas.
Pour l'inspecter ou le sauvegarder, l'opérateur agit sur le volume Docker
(`docker volume inspect bot-radar_alert_chain_state`), jamais sur le dépôt.

## Approbation humaine

L'alerte porte deux boutons, chacun strictement lié à son `alert_id` :

- « Autoriser l'analyse et la préparation d'un correctif — sans merge ni
  déploiement » ;
- « Refuser ».

Une décision n'est enregistrée que si **toutes** les conditions tiennent :
utilisateur dans `TELEGRAM_ALLOWED_USER_IDS`, incident encore **ouvert**, demande
**non expirée** (24 h par défaut), **aucune décision déjà enregistrée**, et
`alert_id` connu. Une décision expirée, refusée, ou liée à un incident résolu
**ne déclenche rien**. Un double clic est refusé et le dit.

L'approbation est **seulement enregistrée** : aucun exécuteur de correction n'est
branché à ce stade, et le message le dit explicitement. Le merge et le déploiement
restent manuels dans tous les cas.

Commandes opérateur, réservées au allowlist :

| Commande | Effet |
| --- | --- |
| `/maintenance_status` | Suspension, incident ouvert, décision, compteur d'échecs |
| `/maintenance_stop` | Suspend les nouveaux traitements de maintenance |
| `/maintenance_resume` | Reprend, sur décision explicite uniquement |

## Suspension (STOP)

`STOP` suspend **uniquement la maintenance**. Le bot métier, PostgreSQL, Redis et
le polling ne sont pas touchés, et le bot ne reçoit aucun privilège système ni
accès au socket Docker.

Sous suspension, le détecteur sort en `0` **sans incident et sans notification**,
et les incidents ouverts sont **conservés**. La reprise exige
`/maintenance_resume` : rien ne reprend tout seul.

La suspension couvre les nouveaux lancements. Elle n'interrompt pas un travail
déjà en cours : aucun travail long n'existe à ce niveau. Pour arrêter aussi le
déclencheur système, l'opérateur agit sur l'hôte, hors du bot :

```sh
# arrêter les déclenchements à venir
sudo systemctl stop botradar-alert-chain.timer

# empêcher toute réactivation au redémarrage
sudo systemctl disable botradar-alert-chain.timer

# état du service et du timer
systemctl status botradar-alert-chain.timer
systemctl list-timers botradar-alert-chain.timer
```

Pour une désactivation durable des deux côtés, suspendre **et** désactiver le
timer. L'un ne remplace pas l'autre : la suspension est logique, le timer est
système.

## Contrôles impossibles

Un contrôle impossible (code `2`, base ou configuration indisponible) **ne clôt
jamais** un incident et ne crée aucun incident métier. Il n'est pas confondu avec
une absence de symptôme.

Le compteur d'échecs consécutifs est persistant. Après **4 échecs consécutifs**,
une **seule** alerte technique distincte est envoyée — vocabulaire séparé, aucune
réutilisation du statut d'incident — puis le silence se poursuit jusqu'à une
reprise réussie. Un contrôle réussi remet le compteur à zéro et réarme l'alerte
technique pour une panne ultérieure. Si l'alerte technique n'est pas délivrée,
elle n'est **pas** marquée comme envoyée : l'opérateur n'est pas trompé.

## Timer (à ne pas activer sans accord)

Activation manuelle uniquement, après revue, et **jamais** par un agent : c'est
une autorisation séparée. Exemple `systemd` (oneshot + timer).

Le volume est attaché au service `app` : la commande ci-dessous ne monte rien et
ne doit rien monter. Un `-v` ajouté ici créerait un état distinct de celui du bot,
donc un `alert_id` différent — exactement le défaut que ce lot corrige.

`/etc/systemd/system/botradar-alert-chain.service`

```ini
[Unit]
Description=BotRadar — contrôle de la chaîne d'alerte (lecture seule)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/bot-radar
ExecStart=/bin/sh -c 'GIT_COMMIT=$(cat .deployed_commit) exec docker compose run \
  --rm --no-deps -T app python scripts/check_alert_chain.py'
```

Le commit est lu dans `.deployed_commit`, que `make deploy` écrit à chaque
livraison : le relevé atteste donc le code réellement déployé. Une valeur figée
dans l'unité deviendrait fausse au déploiement suivant, et le message prétendrait
analyser un commit qui n'est plus celui qui tourne. Sans cette variable, le commit
vaut `inconnu` — l'image ne contient pas `.git`.

`/etc/systemd/system/botradar-alert-chain.timer`

```ini
[Unit]
Description=BotRadar — contrôle de la chaîne d'alerte toutes les 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

Alternative `cron` :

```cron
*/15 * * * * cd /home/ubuntu/bot-radar && GIT_COMMIT=$(cat /home/ubuntu/bot-radar/.deployed_commit) /usr/bin/docker compose run --rm --no-deps -T app python scripts/check_alert_chain.py >> /var/log/botradar-alert-chain.log 2>&1
```

Si l'opérateur veut voir le code `1` comme un échec, ne pas ajouter
`SuccessExitStatus` : le service restera `failed` tant que l'incident dure, ce
qui est le comportement attendu. Le `--no-deps` évite de recréer `postgres` et
`redis` (leçon d'invariance du déploiement).

Rappel d'exploitation : un timer actif et une suspension logique sont deux
leviers distincts. Le premier arrête les déclenchements, le second arrête le
traitement. Voir « Suspension (STOP) » pour les commandes d'arrêt.

## Durcissement optionnel : rôle PostgreSQL en lecture seule

Le script ne fait que des `SELECT`. Un rôle dédié réduit le rayon d'action si le
script est compromis. **Cette étape modifie la base de production et exige un
accord explicite distinct ; elle n'est pas exécutée ici.**

```sql
CREATE ROLE botradar_ro LOGIN PASSWORD '<mot-de-passe-hors-dépôt>';
GRANT CONNECT ON DATABASE radar_db TO botradar_ro;
GRANT USAGE ON SCHEMA public TO botradar_ro;
GRANT SELECT ON listings, listing_matches TO botradar_ro;
```

Puis lancer avec `-e DATABASE_URL=postgresql+asyncpg://botradar_ro:...@postgres:5432/radar_db`.

## Coûts et limites

- Coût : une requête SQL par passage, un message Telegram par incident (dédoublonné),
  aucun appel IA, aucun hébergement supplémentaire.
- Fréquence : la surveillance déterministe peut être fréquente (15 min). L'analyse
  de code assistée par IA reste réservée aux incidents réels ou aux changements.
- Limites : ne couvre que l'axe ingestion → notification ; faux positifs possibles
  sur des reprises lentes ; la trace dépend du disque de l'hôte ; le dédoublonnage
  Redis retombe en local si Redis est indisponible.
