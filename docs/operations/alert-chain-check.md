# Contrôle de silence de la chaîne d'alerte (niveau 1)

Statut : procédure actuelle, **lecture seule**. Ce contrôle ne corrige rien et
n'autorise aucune correction. Déploiement manuel du dispositif.

Périmètre : une stagnation entre l'ingestion d'annonces et les notifications
Telegram. Le script [`scripts/check_alert_chain.py`](../../scripts/check_alert_chain.py)
compare, sur une fenêtre glissante, les annonces reçues, les notifications
acquittées et les correspondances encore bloquées après la reprise F03-B.

## Ce que le contrôle fait et ne fait pas

- **Fait** : lit `listings` et `listing_matches`, qualifie l'incident et envoie
  une alerte Telegram via `notify_admins`. Écrit une trace JSONL locale.
- **Ne fait pas** : aucune écriture en base, aucun appel IA, aucune correction,
  aucun déploiement, aucune modification de CI, permissions, secrets ou workflows.
- **Aucun secret** n'apparaît dans le message ni dans la trace : uniquement des
  identifiants internes (`alert_id`, `match_id`, `listing_id`), des états, des
  compteurs et le SHA du commit. Ni titre, ni URL d'annonce, ni donnée personnelle.

Un relevé sans incident **ne prouve pas** l'absence de bug. Ce contrôle ne couvre
ni la collecte Vinted, ni la réception réelle côté Telegram, ni la chaîne des prix,
ni la vivacité du scheduler, ni l'état de PostgreSQL/Redis. Il ne prouve pas non
plus que la base est saine : il prouve qu'un symptôme précis n'a pas été observé.

## Signaux et seuils

| Paramètre | Défaut | Sens |
| --- | --- | --- |
| `--window-hours` | `2` | Fenêtre des annonces et des acquittements |
| `--stuck-hours` | `1` | Âge minimal d'une correspondance bloquée |
| `--min-stuck` | `3` | Nombre de blocages persistants déclenchant l'alerte |
| `--max-sample` | `8` | Identifiants listés dans le message |
| `--dedup-seconds` | `3600` | Fenêtre du dédoublonnage Redis |

Déclenchement, dans cet ordre :

1. **Critique** — des annonces ont été ingérées et **zéro** acquittement.
2. **Avertissement** — des acquittements existent mais `--min-stuck` blocages
   persistent.
3. Sinon rien. **Un silence sans ingestion ne déclenche jamais** : rien n'a été
   collecté, ce n'est pas un incident.

Une correspondance est « bloquée » si `notified_at IS NULL`, créée avant le seuil,
dans un état que la reprise F03-B doit progresser (`ready`, `deferred`, `sending`)
ou `unknown` **avec un reçu exploitable**, et dont `next_attempt_at` est passé.
`notified` et `abandoned` sont terminaux ; `unknown` sans reçu n'autorise aucun
nouvel envoi et n'est pas, à lui seul, un blocage actionnable.

## Exécution manuelle

Depuis le conteneur applicatif (code déjà présent dans l'image) :

```sh
docker compose run --rm --no-deps -T app python scripts/check_alert_chain.py
```

Simulation sans notification ni écriture de trace :

```sh
docker compose exec -T app python scripts/check_alert_chain.py --dry-run
```

Avec une trace persistée dans un répertoire monté :

```sh
docker compose run --rm --no-deps -T \
  -v /var/lib/botradar:/var/lib/botradar \
  -e ALERT_CHAIN_STATE_FILE=/var/lib/botradar/alert-chain.jsonl \
  app python scripts/check_alert_chain.py
```

Le répertoire monté doit être inscriptible par l'utilisateur du conteneur
(`appuser`, uid `1001`). `--no-state` désactive la trace ; `--dry-run` l'affiche
sans l'écrire et sans notifier.

Codes de sortie : `0` aucun incident, `1` incident détecté, `2` contrôle impossible
(base ou configuration indisponible).

L'image applicative ne contient pas `.git` (le rsync l'exclut) : le commit analysé
vaut donc `inconnu` par défaut dans le conteneur. Pour l'attester, passer
`--commit <sha>` ou `-e GIT_COMMIT=<sha>` — le SHA déployé est affiché par
`make deploy`.

## Trace et statuts

La trace JSONL conserve, pour chaque exécution : `alert_id`, horodatage, seuils,
résultats (compteurs, échantillon), statut et commit analysé. Le script n'écrit
que trois statuts :

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

## Timer (à ne pas activer sans accord)

Activation manuelle uniquement, après revue. Exemple `systemd` (oneshot + timer) :

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
Environment=ALERT_CHAIN_STATE_FILE=/var/lib/botradar/alert-chain.jsonl
ExecStart=/usr/bin/docker compose run --rm --no-deps -T \
  -v /var/lib/botradar:/var/lib/botradar \
  app python scripts/check_alert_chain.py
```

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
*/15 * * * * cd /home/ubuntu/bot-radar && /usr/bin/docker compose run --rm --no-deps -T -v /var/lib/botradar:/var/lib/botradar -e ALERT_CHAIN_STATE_FILE=/var/lib/botradar/alert-chain.jsonl app python scripts/check_alert_chain.py >> /var/log/botradar-alert-chain.log 2>&1
```

Si l'opérateur veut voir le code `1` comme un échec, ne pas ajouter
`SuccessExitStatus` : le service restera `failed` tant que l'incident dure, ce
qui est le comportement attendu. Le `--no-deps` évite de recréer `postgres` et
`redis` (leçon d'invariance du déploiement).

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
