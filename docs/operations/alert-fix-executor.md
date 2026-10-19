# Exécuteur de correction — prototype local `prepare`

Statut : prototype local, sans appel IA réel, sans conteneur, sans push, sans PR,
sans notification et sans accès production. Le niveau 1 n'est ni modifié ni
supposé actif.

## Périmètre

La commande `prepare` transforme un ticket d'approbation synthétique en un
rapport local :

```text
ticket + confirmation
  → preuves expurgées
  → test de reproduction
  → échec fonctionnel contrôlé
  → correctif
  → test vert
  → Ruff, Mypy, recette isolée
  → runs/<ticket_id>-<run_id>/REPORT.md
```

Aucun `publish` n'existe dans ce lot. Aucun push, merge, déploiement, timer ou
notification n'est exécuté.

## Fichiers

- `app/services/alert_fix/core.py` : modèles, ticket, hash, TTL, ledger, rapport.
- `app/services/alert_fix/policy.py` : allowlist positive, chemins protégés,
  plafonds de diff, classification stricte, expurgation.
- `app/services/alert_fix/runner.py` : runner local, copies jetables, environnement
  par allowlist, limites et timeouts.
- `app/services/alert_fix/prepare.py` : orchestration et écriture du rapport.
- `scripts/alert_fix.py` : CLI `prepare`.
- `scripts/alert_fix_rehearsal.py` : rehearsal locale avec bug synthétique.
- `tests/alert_fix_fixtures.py` : ticket, propositions et patchs synthétiques.
- `tests/unit/test_alert_fix_prepare.py` : tests du lot.
- `docs/operations/alert-fix-executor.md` : cette page.

## Ticket

Le ticket au schéma 2 est un JSON strict, à hash canonique. Il contient `ticket_id`,
`alert_id`, l'incident, la décision `approuvé`, `issued_at`/`expires_at`,
`base_sha`, `allowed_files`, `test_file`, `evidence_files`, `evidence_hashes`,
`provenance`, `trace` et `limits`.

- `ticket_hash` est un SHA-256 d'intégrité, pas une signature.
- `prepare` exige `--confirm-prepare` et `--confirm-ticket-hash <hash>`.
- TTL par défaut 600 s, plafond de 4 h.
- Le ledger local `used_tickets.jsonl` est append-only et verrouillé ; un ticket
  n'est consommé qu'une fois.
- `--allow-file` ajoute explicitement un chemin sous racine dure et hors chemins
  protégés ; le ticket ne peut jamais augmenter les plafonds.
- Le ticket ne contient pas de chemin de dépôt : `--repo` est fourni par
  l'opérateur, et `HEAD` doit égaler `base_sha`.

## Allowlist et plafonds

La liste positive accepte `app/**` et `docs/**`, plus un seul nouveau fichier de
test sous `tests/unit/` ou `tests/integration/`. Aucun glob.

Sont refusés : `.github`, `.git`, `compose.yml`, `Dockerfile`, `alembic`,
`pyproject.toml`, `requirements.lock`, `Makefile`, `.env`, `tests/conftest.py`,
`scripts/verify_consolidation.py`, `scripts/check_alert_chain.py`,
`app/services/alert_chain_state.py`, `app/services/alert_fix/**`, `app/bot/**`,
`app/providers/llm/**` et les tests existants.

Plafonds : 3 fichiers modifiés, 120 lignes, 32 Kio, 2 propositions, 30 minutes.

## Preuves et propositions

Les preuves se limitent à la trace du détecteur, aux compteurs, IDs internes,
états, horodatage et fichiers explicitement autorisés. Aucun titre, URL, log brut,
PII, cookie, token ou secret.

Les données sont placées entre `<donnees_non_fiables>` et précédées d'une
instruction système qui refuse toute consigne qu'elles contiendraient. La politique
et les limites restent dans le code de l'hôte : une donnée ne peut pas les changer.

Les propositions sont enregistrées dans un JSON local (`test` puis `fix`). Le
chemin LLM existe, mais refuse l'exécution avec `USAGE_UNAVAILABLE` tant que
l'usage et le coût ne sont pas mesurables. Le rapport porte donc :

```json
"llm": {"mode": "recorded", "calls": 0, "proposals": 2, "model": null, "usage": null, "cost_usd": null}
```

## Test de reproduction

La classification ne se contente pas du code de sortie. Le rapport final de
`verify_consolidation.py` doit être valide et :

- exactement un test collecté, dont le nodeid correspond au fichier ;
- avant correctif : au moins un `FAILED` en phase `call`, `errors = 0`, code 1 ;
- après correctif : `failed = 0`, `passed >= 1`, code 0 ;
- aucune erreur de collecte, d'import, de syntaxe ou de dépendance ;
- une sortie bornée et expurgée.

Une erreur `ERROR`, une collecte vide ou un test qui passe avant correctif est
refusé.

## Runner local

`LocalRunner` copie le workspace dans un répertoire jetable, crée un `.env` vide
(taille 0, listé dans `infra_files`) et exécute :

```text
python -B scripts/verify_consolidation.py unit --target <test>
ruff check . --cache-dir <run>/cache/ruff
mypy --cache-dir <run>/cache/mypy app scripts/matching_diagnostics.py
python -B scripts/verify_consolidation.py unit --target tests
```

L'environnement enfant est une allowlist : `PATH`, `PYTHONPATH`, `HOME` et
`TMPDIR` sous le run, `LANG`, `LC_ALL`, `PYTHONDONTWRITEBYTECODE`,
`PYTHONUNBUFFERED`, `TZ`. Aucun `DATABASE_URL`, `TELEGRAM_*`, `AI_*`,
`GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`, `DOCKER_HOST`, proxy ou secret.

Le runner borne la sortie, applique un timeout global de 30 minutes et un timeout
par commande, tue le groupe de processus, et compare un hash du workspace avant
et après chaque exécution. Toute modification invalide le run. Il applique des
limites `resource` en best-effort et consigne celles qui ont été acceptées.

**Limite honnête** : c'est une isolation logique, pas une sandbox noyau.
L’environnement filtré ne transmet pas de secret, mais les sous-processus
conservent les droits système de leur utilisateur et peuvent accéder au réseau ou
aux fichiers de l’hôte. Le runner ne peut pas empêcher un
code malveillant de coder en dur un chemin du système. Le conteneur réel est un
lot séparé.

## Rapport

`run.json` et `REPORT.md` sont écrits atomiquement dans
`runs/<ticket_id>-<run_id>/`. Le rapport contient :

- `ticket_id`, `alert_id`, `run_id`, `status`, `reason_code` ;
- fichiers autorisés et modifiés, cause proposée ;
- test avant/après, contrôles, diff ;
- `llm.mode`, `usage`, `cost_usd` ;
- limites effectives, incertitudes, `.env` créé ;
- `publication: "none"` et décision.

Statuts : `ready_for_review`, `out_of_scope`, `reproduction_failed`,
`fix_failed`, `control_failed`, `ai_error`, `runner_error`, `timeout`,
`workspace_modified`.

## Rehearsal

Le rehearsal clone localement le dépôt, injecte un bug synthétique dans le clone,
construit un ticket, écrit les propositions enregistrées, exécute `prepare`,
vérifie le rapport et rejoue le même ticket pour prouver le refus.

```sh
python scripts/alert_fix_rehearsal.py --keep
```

Il vérifie notamment : ticket consommé une fois, test rouge fonctionnel avant,
vert après, contrôles verts, rapport `ready_for_review`, `.env` vide listé,
`publication: "none"`, `llm.mode: "recorded"`, aucune nouvelle branche, aucun
commit/push/PR/notification, dépôt source inchangé.

Le clone est supprimé sans `--keep`. Le répertoire d'état et le rapport sont
conservés pour revue.

## Validation

```sh
ruff check .
mypy app scripts/matching_diagnostics.py
make test-isolated
python scripts/alert_fix_rehearsal.py
```

## Lots suivants

1. `publish` : réutiliser le candidat et le diff préparés, avec confirmation
   humaine distincte ; jamais de merge, déploiement, force-push ou `--admin`.
2. Exportateur de ticket depuis l'état durable du niveau 1, avec claim atomique,
   TTL et usage unique.
3. Déclenchement après clic Telegram : worker dédié sur une machine non-production,
   jamais le bot lui-même, jamais un shell privilégié sur le VPS. Le timer du
   niveau 1 n'est pas supposé actif.

La clôture reste décidée par le détecteur sur absence de symptôme, jamais par
l'exécuteur, un merge ou un déploiement.


## Socle déterministe sans activation

`app/services/alert_fix/exchange.py` exporte un ticket depuis un incident ouvert
et une décision du journal durable, avec contrôle du décideur autorisé, de la
suspension, du SHA complet et du périmètre fourni par l'opérateur. Le journal
`tasks.jsonl`, sous le verrou d'état existant, réserve une seule tâche par décision.
Les transitions réservée/démarrée/interrompue sont durables. Une reprise exige
confirmation de l'arrêt de la tentative précédente puis autorisation explicite ;
elle ne contourne pas les vérifications effectuées au démarrage.

Trois délais sont distincts : demande d'approbation (24 heures après ouverture),
validité de l'approbation depuis la décision (paramètre opérateur, maximum 24 h),
et ticket (maximum 4 h, borné par la validité de l'approbation).

L'enveloppe est authentifiée par HMAC-SHA256 avec une clé dédiée d'au moins
32 octets, fournie par l'hôte de confiance. Aucune clé réelle n'est créée ou
configurée par ce lot. Le worker futur devra vérifier cette enveloppe et obtenir
la validation de démarrage auprès de l'autorité ; vérifier le hash ou appeler
`is_approved` ne suffit pas. Le répertoire d'autorité doit être protégé en écriture
contre le candidat. Aucune commande, queue réseau ou service worker n'est activé.

Les rapports distinguent appels IA et propositions. Une source `recorded`
effectue zéro appel IA. Usage et coût absents restent non mesurés. Un ticket JSON
seul porte une provenance non vérifiée ; il ne devient pas une preuve de
production. L'isolation rapportée dépend du runner effectivement utilisé.

`prepare` refuse par défaut le runner hôte. `--trusted-recorded` est réservé aux
fixtures enregistrées de confiance, dont le rehearsal fourni. Cette option ne
constitue pas une sandbox et ne doit jamais servir à exécuter une proposition IA.
Le chemin IA reste désactivé.

Avant tout raccordement réel : exécuteur jetable sans réseau, secrets, home,
accès production ou socket Docker ; dépendances préinstallées et ressources
bornées ; génération séparée avec sa seule clé IA ; contrôleur de résultats hors
candidat. L'hôte, les appels et les budgets restent à approuver.

La publication future exigera une confirmation distincte portant sur le SHA de
base, les empreintes du candidat et du rapport testé. Elle devra appliquer ce
candidat sur la vraie base Git, sans publier l'histoire indépendante du rehearsal.
Aucune commande de publication n'est disponible à ce stade.


## Contrat de contexte au schéma 2

Les tickets de schéma 1 sont refusés avec une demande de réexport explicite ;
ils ne sont jamais réinterprétés. Les propositions enregistrées utilisent aussi
le schéma 2.

`allowed_files` autorise les modifications. `evidence_files` autorise uniquement
la consultation, avec une empreinte SHA-256 dans `evidence_hashes` pour chaque
fichier. Les deux listes, les empreintes et le SHA complet de base sont confirmés
ensemble par l'opérateur : `confirmed_scope_hash` à l'export et le hash du ticket
à la préparation. L'enveloppe authentifiée englobe toutes ces données. Toute
modification invalide confirmation et authentification. Les plafonds et chemins
protégés en écriture restent imposés par le code, hors du contrôle du ticket.

Le contexte est limité à huit fichiers source ou documentation sous les racines
permises, 20 000 octets par fichier et 64 000 octets au total. Chemins absolus,
traversées, répertoires privés et secrets, fichiers `.env` et liens symboliques
sont refusés. Les fichiers viennent de l'archive du SHA approuvé et leurs
empreintes sont comparées avant préparation des propositions. Le contexte est
expurgé avec les règles existantes de masquage des secrets ; aucun journal brut,
état de session ou fichier privé n'est une source de contexte autorisée.

`provenance` déclare l'origine (`synthetic_fixture`, `approved_state` ou
`unverified`). Cette déclaration n'est pas une preuve. La commande `prepare`
autonome rapporte une provenance non vérifiée ; la future intégration devra
consommer l'enveloppe authentifiée et la validation de démarrage existantes.
