# Migration IA : River actif par défaut, Luna disponible

Mise à jour : 25 octobre 2026. Base locale : `9fe331ee54fe920baf23cc0c472b38fc88374d03`.
Branche de travail : `feat/migration-gpt6-luna`.

Cette livraison prépare la migration et sa validation locale. Les premiers essais
réels ont été suivis d'une passe de dix appels, techniquement valide mais de qualité
métier inégale. Une revalidation ciblée de sept appels a confirmé la compatibilité
technique et l'abstention du scoring ; la qualité des variantes reste inégale.
L'exemple final de variantes a ensuite été aligné sur la liste vide, sans nouvelle
passe payante. Aucune bascule de production, aucun redémarrage, commit ou push.
Le respect de ce dernier changement par le modèle reste non vérifié.
Le contrat canonique reste [Telegram et IA](../contracts/telegram-ai.md#frontière-ia-et-absence-de-réponse).

## Résumé et architecture avant/après

Les cinq usages sont conservés : `/add`, variantes/exclusions, scoring post-alerte,
négociation et auto-tuning. L'IA reste consultative ; aucune action d'achat n'est ajoutée.

La migration initiale vers Luna est conservée : abstraction, validation Pydantic stricte,
Structured Outputs, télémétrie, replis et corrections de bugs. Le changement de cible
ne modifie aucun module métier ni prompt et ne reconstruit pas la branche.

Le défaut devient River, qui sert et facture `deepseek-ai/DeepSeek-V4.1-Flash-1017`.
Le protocole utilisé est Chat Completions compatible OpenAI ; le fournisseur
journalisé est `river`, jamais `deepseek` ou `openai` pour ces appels.

Cinq adaptateurs sont enregistrés : `river`, `openai`, `deepseek`, `openai-compatible`,
`anthropic`. Un seul est construit comme moteur actif par la configuration.
`RiverClient` réutilise le dialecte structuré existant : sa spécialisation est
limitée à l'identité du fournisseur et à `max_tokens`, à la place du champ officiel
OpenAI `max_completion_tokens`. Transport, parsing, schémas et diagnostics restent
communs. Le générique seul ne fournit pas ces schémas stricts ni le réglage de
raisonnement ; la spécialisation évite des paramètres ou flags supplémentaires.

Aucun SDK ni dépendance ajouté. Aucun routage ou fallback vers un fournisseur.
Luna reste intégré et testé, mais inactif tant que `AI_PROVIDER=river`.
Un protocole nouveau demanderait un adaptateur ; passer entre adaptateurs
existants ne demande aucun changement métier ni migration de données.

## Configuration finale

Dans un environnement dédié, définir :

```dotenv
AI_PROVIDER=river
AI_MODEL=deepseek-ai/DeepSeek-V4.1-Flash-1017
AI_API_KEY=<CLE_RIVER_A_RENSEIGNER_HORS_DEPOT>
AI_BASE_URL=https://api.river.ai/v1
AI_TIMEOUT_SECONDS=12.0
AI_MAX_TOKENS=2048
AI_REASONING_EFFORT=none
AI_ENABLED=true
```

Renseigner `AI_API_KEY` par le canal de secrets de cet environnement. Ne pas coller
la clé dans une commande, un compte rendu ou un fichier versionné. Conserver les
cinq flags `AI_FEATURE_*` selon les usages voulus.

Le POST cible `https://api.river.ai/v1/chat/completions`. Le plafond devient
`max_tokens`, le raisonnement `reasoning_effort=none`. Aucun `thinking`,
`max_completion_tokens` ou paramètre Responses propre à Luna n'est envoyé.
Les propriétés de sortie sont requises sur le fil, avec `null` pour les champs
qui l'autorisent et `additionalProperties=false`. Les défauts métier restent
locaux. Le plafond de 2 048 tokens a suffi aux sept appels ciblés sans troncature ;
ce petit échantillon ne garantit pas les autres entrées.

Une complétion produit un seul POST. Pas de retry, même sur 429, ni de redirection
HTTP automatique. Les 12 secondes sont le timeout HTTP configuré, pas une borne
sur toute l'opération métier ou la résolution bibliographique.

## Contrat River vérifié dans la documentation officielle

Consultation le 24 octobre 2026, sans appel d'inférence ni sonde réseau réelle.

| Élément | Contrat retenu et source |
| --- | --- |
| HTTP et authentification | Base `https://api.river.ai/v1`, POST `/chat/completions`, Bearer et JSON : [quickstart](https://docs.river.ai/guides/inference/) |
| Modèle exact | `deepseek-ai/DeepSeek-V4.1-Flash-1017`, distinct du modèle sans suffixe : [modèles](https://docs.river.ai/guides/inference-models/) |
| Limites annoncées | Contexte 1 048 576 tokens, sortie maximale 384 000 ; BotRadar conserve son plafond 2 048 : [modèles](https://docs.river.ai/guides/inference-models/) |
| Sortie structurée | `json_object` et `json_schema` avec `strict=true` sont documentés pour Chat Completions. Les cinq usages gardent le schéma strict avant le POST, puis Pydantic : [contrôles](https://docs.river.ai/guides/inference-controls/) |
| Génération | `max_tokens`, `temperature` entre 0 et 2 ; effort `none`, `high`, `max` pour la famille Flash : [contrôles](https://docs.river.ai/guides/inference-controls/) |
| Réponse | Texte `choices[0].message.content`, compteurs dans `usage` : [quickstart](https://docs.river.ai/guides/inference/) |
| Santé | GET `/v1/models` documenté ; sonde existante en cache 30 s, sans génération, découverte de capacités ou vérification de crédits : [quickstart](https://docs.river.ai/guides/inference/) |
| Erreurs | 400/422 requête, 401/403 accès, 402 solde, 404 endpoint/modèle, 413 taille, 429/503 capacité, 500/502/504 service : [limites](https://docs.river.ai/guides/inference-limits/) |

Le tableau de raisonnement nomme `DeepSeek-V4.1-Flash` sans détailler séparément la
révision `1017`. Le choix `none` applique ce contrat de famille. Le premier test
réussi et la passe de dix appels ont ensuite observé son acceptation sur cette
révision et les schémas utilisés, avec zéro token de raisonnement retourné. Les
sorties strictes restent documentées globalement, sans matrice exhaustive par
révision ou variante de schéma. Ces observations ne constituent pas une garantie
pour toute requête ; aucune tentative automatique dans un autre mode.

Les pages d'inférence ne garantissent pas un schéma exhaustif des corps d'erreur
HTTP. Le traitement classe le statut sans dépendre de leur structure ni imprimer
leur contenu. Il ne transpose pas les classes d'erreur gRPC du SDK River à HTTP.
River recommande des relances dans certains cas ; BotRadar conserve son contrat
plus strict : un seul POST, aucune relance, aucun changement de mode ou de moteur.

Le SDK River n'est pas utilisé. L'inspection de HTTPX 0.28.1 installé confirme que
`AsyncHTTPTransport` a `retries=0` par défaut et qu'`AsyncClient` n'en modifie pas la
valeur lors de sa construction standard. Le HTTP partagé existant suit ce chemin.
Les tests comptent les requêtes réelles du transport simulé, y compris sur 429.

Selon la [table d'usage River](https://docs.river.ai/guides/inference-models/),
`prompt_tokens` inclut le cache et `completion_tokens` inclut le raisonnement.
Le mapping est conservé : `usage.input`, `usage.output`, `usage.cached`,
`usage.reasoning`. Les deux derniers sont des sous-ensembles, jamais ajoutés aux
totaux ; un compteur absent reste inconnu. Aucun tarif, coût ou montant estimé.

## OpenAI/Luna conservé pour une bascule ultérieure

Ce bloc remplace intégralement le bloc actif quand la bascule sera autorisée :

```dotenv
AI_PROVIDER=openai
AI_MODEL=gpt-6-luna
AI_API_KEY=<CLE_OPENAI_A_RENSEIGNER_HORS_DEPOT>
AI_BASE_URL=https://api.openai.com/v1
AI_TIMEOUT_SECONDS=12.0
AI_MAX_TOKENS=2048
AI_REASONING_EFFORT=none
AI_ENABLED=true
```

Même configuration unique, aucun second jeu de clés. OpenAI garde
`max_completion_tokens`, `reasoning_effort=none` et les Structured Outputs stricts.
La spécialisation River ne change pas le corps de la requête Luna. Les tests
OpenAI et les scénarios Telegram de l'adaptateur alternatif sont conservés.
Changer ensemble fournisseur, modèle, URL et clé, recréer le seul service
applicatif puis vérifier santé et qualité. Ne pas laisser la clé River avec l'URL
OpenAI, ni faire de comparaison automatique entre fournisseurs.

## Corrections et couverture de non-régression

| Correction | Preuve synthétique |
| --- | --- |
| Paramètre DeepSeek présent dans le dialecte générique | Requêtes par adaptateur ; absence de `thinking` pour OpenAI et le générique |
| Objets/nombres convertibles en mots-clés | Refus des éléments non textuels dans les payloads ; auto-tuning sans exclusion inventée |
| Rationale brut dans les logs | Sentinelle absente des logs sérialisés |
| JSON non standard accepté par le parseur Python | Rejet de `NaN`, `Infinity`, `-Infinity` |
| Absence de statut de requête incluant la validation | Statut final après parsing et validation, refus et troncature inclus |
| Risque de masquage des nouveaux compteurs par les noms contenant `token` | JSONFormatter conserve `usage.input/output/cached/reasoning` et masque les secrets |
| Corrélation des diagnostics entre appels superposés | Deux requêtes superposées gardent identifiant, module et statut distincts |
| Mesure de latence contournant les adaptateurs | Sonde passant par `LLMClient`, schéma et identifiant de module vérifiés |
| Repli de négociation pouvant compter comme succès du moteur dans l'évaluation | Une panne HTTP reste un échec de la sonde malgré l'offre déterministe |
| Ressource HTTP laissée ouverte par les sondes | Fermeture en `finally`, y compris après exception |

La comparaison à réponse identique vérifie les cinq adaptateurs et les cinq
payloads, sans ajout de défauts ni transformation du dictionnaire rendu au métier.
Les réponses vides valides, listes `null`, champs optionnels et marges négatives
restent couverts. Le rejet de listes contenant des objets ou nombres est intentionnel :
la réponse entière est refusée et le repli normal s'applique, sans conversion en chaîne.

Le test de livraison suspend la génération par événement asyncio : l'accusé Telegram
arrive d'abord. Timeout, 429, contenu vide, JSON invalide, validation refusée et
troncature ne retirent pas cet accusé. Le succès répond au vrai `message_id`.

Les suites de référence sont `test_llm_contract.py`, `test_llm_migration.py`,
`test_ai_validation.py`, `test_ai_subsystem.py`, `test_delivery_outcomes.py` et les
tests des deux sondes. La couverture utile et les tests historiques sont conservés.

## Retraits de code justifiés

Aucun fichier supprimé et aucun test utile retiré. Les retraits ci-dessous
appartiennent à la migration initiale et restent conservés ; le changement River
retouche les défauts, la spécialisation de protocole, les tests et la documentation.

- Le champ `thinking` quitte le générique et reste dans le seul adaptateur DeepSeek.
  Sa recherche active se limite au dialecte et aux tests de ce contrat.
- La construction HTTP, les comparaisons de raisonnement et les impressions de
  corps externes de la sonde de latence sont remplacées par le chemin commun.
  Ces branches appartenaient au seul script ; sa commande est conservée et testée.
- Le log du rationale est retiré : il n'alimente aucun comportement métier et expose
  du contenu modèle inutile au diagnostic. Les statuts et compteurs le remplacent.
- La boucle croisée des fragments post-alerte est remplacée par une association
  clé/module. Le prompt partagé est réutilisé par la sonde, sans second dialecte.
- Les noms DeepSeek de helpers génériques deviennent neutres ; tous leurs
  consommateurs sont adaptés, sans changement des scénarios ni suppression de test.

Les registres existants, les branches de compatibilité encore consommées et les
ADR historiques ne sont pas supprimés. Dockerfile, Compose, dépendances, formules
de prix et traitements marketplace restent hors du diff fonctionnel.

## Tests locaux et portée des preuves

Utiliser l'interpréteur déjà vérifié ou un environnement autorisé avec le verrou
existant. Dans le worktree sans venv propre, passer son chemin par `PYTHON` à Make.
Aucun import applicatif de vérification ne doit lire un `.env` réel.

```sh
ruff check .
python -B scripts/verify_consolidation.py typecheck
make test-isolated
```

Le mode `typecheck` neutralise la configuration et les transports avant mypy sur
`app` et `scripts/matching_diagnostics.py`, puis sur les scripts opérationnels
supplémentaires de la recette existante. `make test-isolated` utilise la même
isolation pour les tests et leurs sous-processus. Les refus de transport sont
exercés par la sonde du lanceur ; aucun appel River ou OpenAI n'est nécessaire.

Les tests PostgreSQL ignorés ne constituent pas une validation PostgreSQL.
Aucun contrôle distant, job CI, appel payant ni service réel n'est attesté.
Les résultats exacts de la dernière validation figurent dans le bilan ci-dessous.

## Procédure de validation réelle et historique des essais

Aucun nouvel appel payant n'est autorisé à ce stade. Les commandes suivantes sont à
exécuter uniquement après mandat explicite, dans un shell dédié où l'exploitant
injecte lui-même `AI_API_KEY` par son dispositif sécurisé. Ne pas utiliser `set -x`,
`env`, `printenv`, un fichier `.env`, les accès de production ou le démarrage du bot.
Le venv existant doit disposer des dépendances verrouillées. Aucun service local,
Telegram, PostgreSQL ou Redis n'est nécessaire pour le premier test.

### Diagnostic d'authentification après le premier appel réel

Bilan transmis par l'exploitant : **un POST, HTTP 401, 323 ms**, aucune sortie
métier ni consommation disponible. Ce résultat est rapporté, pas reproduit ici.
Le client a reçu un refus HTTP d'authentification ; la raison précise reste
inconnue. Les indicateurs corrigés sont `json_schema_strict_accepte: null`,
`reasoning_effort_none_accepte: null` et `validation_pydantic: null`.
L'ancien `AssertionError` venait du contrôle attendant HTTP 200, après l'erreur
HTTP d'origine ; il ne constitue pas une deuxième cause d'échec.

Le bloc précédent exportait bien `AI_API_KEY` vers Python. `Settings` lit cette
variable en `SecretStr`, puis la factory construit `RiverClient` sans argument
remplaçant la clé. Aucun faux client n'est chargé dans ce chemin manuel. Le
singleton est créé après les exports, dans un nouveau processus. L'URL construite
est exactement `https://api.river.ai/v1/chat/completions` et l'adaptateur ajoute
une seule chaîne `Bearer ` devant la valeur saisie. Une clé collée avec ce préfixe
produirait donc un double préfixe ; des guillemets collés restent littéraux.

La reproduction zsh avec entrées factices montre que l'ancien `read -rs` peut
retirer les espaces périphériques selon `IFS`, mais conserve guillemets, préfixe
collé et retour chariot reçu dans la ligne. Une lecture s'arrête au saut de ligne :
elle ne permet pas de certifier qu'un collage multiligne n'a rien laissé dans
le terminal. La nouvelle saisie utilise `IFS= read -rs` depuis `/dev/tty`, conserve
la valeur et refuse toute anomalie détectée, sans trim ni correction. Coller
uniquement la valeur brute d'une clé sur une ligne, sans guillemets ni `Bearer`.
Ces possibilités de saisie ne démontrent pas la cause du 401 observé.

Dans la console River, vérifier sans transmettre la clé : son état actif et non
révoqué/expiré si ces états sont proposés, sa création pour le service d'inférence,
le compte ou l'équipe/projet propriétaire et les droits d'accès associés. Vérifier
que la clé copiée correspond à cette entrée, pas à un autre service ou compte.
Aucune modification de modèle, schéma, raisonnement ou architecture n'est justifiée
par ce seul 401. Aucun nouvel appel distant n'a été effectué pour ce diagnostic.

### Preuves restantes et prochaine collecte diagnostique

Le premier programme ne sauvegardait aucun corps ni en-tête de réponse. Son hook
n'extrayait que `error.param` parmi quatre noms autorisés ; aucun n'apparaît dans
le bilan transmis. Le client attrape `HTTPStatusError`, garde le statut et la
catégorie, puis rend `None`. Le processus Python est terminé. Aucun code, message,
en-tête d'authentification ou identifiant River n'est disponible dans les éléments
conservés ici. Aucun fichier ou log n'a été fouillé pour retrouver une clé ou une
réponse supposée. Une copie éventuellement conservée par l'exploitant n'a pas été
fournie ; son existence n'est pas présumée. Le `request_id` UUID des logs BotRadar
est généré localement et n'est pas le `x-request-id` du fournisseur.

Documentation officielle consultée pour ce diagnostic :

- [Authentification HTTP](https://docs.river.ai/guides/inference/) : clé créée dans
  la Console, URL de base `https://api.river.ai/v1`, en-tête `Authorization: Bearer`.
  Le nom `RIVER_API_KEY` des exemples n'est pas imposé par HTTP ; BotRadar transmet
  sa variable unique `AI_API_KEY` à cet en-tête.
- [Erreurs et support](https://docs.river.ai/guides/inference-limits/) : pour
  401/403, vérifier clé et accès compte/modèle ; le solde est mentionné sous 402.
  Cela ne prouve ni la validité de la clé observée, ni l'état des accès ou du solde.
  River demande heure/fuseau, modèle, endpoint, statut et `x-request-id` si présent.
- [Premier accès](https://docs.river.ai/quickstart/) : vérifier que la clé appartient
  au compte ou à l'équipe voulus. Aucun détail documenté ne permet de départager
  les causes particulières d'un 401 à partir du seul statut.

`GET /v1/models` est documenté, mais les pages consultées ne garantissent pas
explicitement son absence de facturation. Les méthodes de santé/capacités du SDK
River n'établissent pas non plus les trois conditions requises ici pour choisir
un nouvel endpoint gratuit. Aucun essai de ces chemins n'est proposé.

**Prochaine requête préparée, à autoriser séparément :**

| Élément | Engagement |
| --- | --- |
| Endpoint | `POST https://api.river.ai/v1/chat/completions` |
| Budget | 1 requête HTTP maximum, aucun GET préalable, retry, redirection ou fallback |
| Facturation | Inférence potentiellement facturable ; aucune gratuité du diagnostic ou des erreurs n'est présumée |
| Modèle/contrat | Même identifiant `1017`, `NegotiationPayload`, schéma strict, effort `none`, plafond 2 048 |
| Collecte | Heure Europe/Paris avec décalage UTC, statut, code/type/message sélectionnés, `x-request-id`, schéma et code d'erreur de `WWW-Authenticate` si présents ; latence et usage existants |

Le bloc ci-dessous reste en mode local (`BOTRADAR_AUTH_LOCAL_ONLY=1`). Sa branche
réseau, préparée avec `0`, ajoute uniquement un hook de lecture à la même requête.
Il ne modifie pas le client applicatif. Les champs JSON sont lus seulement si le
corps d'erreur est un JSON de taille limitée et si leur structure est reconnue ;
aucun format exhaustif d'erreur River n'est inventé. Un corps non JSON est signalé,
pas affiché. Ni Authorization, Cookie, Set-Cookie, autres en-têtes, requête complète,
variables d'environnement ni corps brut ne sont enregistrés.

Les textes externes ne sont pas imprimés dans le bilan. Les seuls champs
sélectionnés passent par la rédaction existante, le masquage des clés reconnaissables
et l'omission de tout champ contenant un fragment d'au moins quatre caractères de
la clé saisie. Ils sont bornés et écrits dans `river-diagnostic-local.json`, créé
exclusivement avec droits 0600 dans le dossier temporaire privé. Ce filtrage conservateur peut aussi masquer un identifiant légitime en cas
de coïncidence de caractères avec la clé. Il ne certifie pas qu'un message libre est partageable : une donnée transformée ou un
fragment plus court ne peut pas être exclu automatiquement. Le dossier est conservé
s'il contient ce diagnostic ; aucun fichier de configuration n'est modifié.

**Consultation locale :** ouvrir uniquement ce petit fichier de champs sélectionnés
dans un éditeur texte local, sans synchronisation ni partage. Les caractères de
contrôle sont échappés par JSON ; traiter le texte comme des données non fiables,
jamais comme des commandes. Ne pas coller le fichier dans la conversation. Examiner
code/type/message et l'identifiant ; ne transmettre que les éléments relus et
vérifiés exempts de secret, même partiel. Si le message reste douteux, le garder
localement et demander à River une explication à partir de l'heure, du statut et
de l'identifiant vérifié. L'absence de champ est une information manquante, pas la
preuve que River n'en avait émis aucun dans une autre forme.

### Conclusion du diagnostic au niveau de preuve actuel

| Niveau | Conclusion et preuve |
| --- | --- |
| Faits observés, rapportés par l'exploitant | 1 POST, HTTP 401, 323 ms ; aucun résultat métier ou usage disponible |
| Défauts locaux écartés dans le chemin inspecté | Mauvaise URL, absence d'export, substitution par une clé factice et double préfixe ajouté par le code : inspection et vérifications factices ; la valeur réellement saisie reste inconnue |
| Cause confirmée | Aucune cause précise du refus fournisseur ; seul le refus HTTP est établi |
| Causes documentées à vérifier | Clé et accès compte/modèle selon River ; compte/équipe selon le quickstart |
| Hypothèses non démontrées | Clé révoquée/erronée, mauvais compte/équipe, accès manquant, saisie altérée ou refus générique d'une couche d'accès ; crédits épuisés non déductibles du 401 |
| Preuve discriminante suivante | Champs sélectionnés et `x-request-id` de la même requête après autorisation ; message spécifique à confronter à la Console, sinon corrélation par River |

Si la prochaine réponse reste générique, même avec un identifiant, la cause n'est
pas exposée par l'API et nécessite une confirmation fournisseur. Brouillon de
demande, **à envoyer soi-même uniquement après revue**, à `support@river.ai`
(adresse indiquée par la documentation officielle) :

```text
Objet : Refus HTTP 401 sur l'API d'inférence

Bonjour,
Une requête unique, sans retry, a reçu HTTP 401.
Date et heure avec fuseau/décalage UTC : <heure relevée, ne pas inventer celle du premier appel>
Endpoint : POST https://api.river.ai/v1/chat/completions
Modèle : deepseek-ai/DeepSeek-V4.1-Flash-1017
Statut : 401
x-request-id : <identifiant relu localement, ou « non disponible »>
Code/type fournisseur : <valeurs relues, ou « non disponibles »>
Message : <facultatif, uniquement après revue et expurgation>

Pouvez-vous corréler cette requête et confirmer la raison précise du refus :
reconnaissance de la clé, rattachement au compte/équipe et accès à l'inférence/modèle ?
La réponse disponible ne permet pas de départager ces causes.
Aucune clé, aucun en-tête d'authentification ni corps brut n'est joint.
```

### Bloc autonome : contrôle local par défaut, zéro requête

Le bloc ci-dessous demande une nouvelle saisie masquée dans un sous-shell et ne
lit aucun `.env` réel. Il vérifie la présence de la clé et ses caractères suspects,
la concordance environnement/configuration/client, l'URL exacte et l'en-tête avec
une clé factice. Il n'affiche ni clé, fragment, empreinte ni en-tête complet.
Une saisie suspecte produit `STOP` et demande une nouvelle saisie ; rien n'est corrigé.

**La valeur `BOTRADAR_AUTH_LOCAL_ONLY=1` interdit ici tout appel.** Pour le prochain
test réel préparé, remplacer uniquement `1` par `0`, après nouvelle autorisation
explicite. Le chemin réseau conserve le même client, prompt et `NegotiationPayload`,
avec un seul POST au maximum, sans healthcheck, retry, autre mode ou fournisseur.
Le hook vérifie aussi l'unicité de l'en-tête réellement envoyé. Les résultats
HTTP et les erreurs du contrôle sont désormais deux champs distincts du bilan.

```zsh
(
  set +x
  unsetopt VERBOSE
  set -e
  unset PYTHONOPTIMIZE
  cd /Users/aminezegmou/Projets/bot-radar-worktrees/migration-gpt6-luna
  export BOTRADAR_SOURCE="$PWD"
  export BOTRADAR_PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
  [[ -x "$BOTRADAR_PYTHON" ]] || { print -u2 'STOP : interpréteur absent.'; exit 1; }
  IFS= read -rs 'AI_API_KEY?Clé River brute (saisie masquée) : ' </dev/tty
  print
  export AI_API_KEY
  export PYTHONPATH="$BOTRADAR_SOURCE"
  export APP_ENV=development
  export AI_PROVIDER=river
  export AI_MODEL=deepseek-ai/DeepSeek-V4.1-Flash-1017
  export AI_BASE_URL=https://api.river.ai/v1
  export AI_TIMEOUT_SECONDS=12.0
  export AI_MAX_TOKENS=2048
  export AI_REASONING_EFFORT=none
  export AI_ENABLED=true
  export BOTRADAR_AUTH_LOCAL_ONLY=1
  BOTRADAR_MANUAL_DIR="$(mktemp -d /tmp/bot-radar-river-manual.XXXXXX)"
  trap 'rmdir "$BOTRADAR_MANUAL_DIR" 2>/dev/null || true' EXIT
  cd "$BOTRADAR_MANUAL_DIR"
  "$BOTRADAR_PYTHON" -B - <<'PY_RIVER_PREFLIGHT'
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logging.getLogger().handlers = [logging.NullHandler()]
logging.getLogger().setLevel(logging.CRITICAL)

MODEL = 'deepseek-ai/DeepSeek-V4.1-Flash-1017'
ENDPOINT = 'https://api.river.ai/v1/chat/completions'
records = []
requests = []
error_fields = []
river_diagnostic = {}
key = os.environ.get('AI_API_KEY', '')
auth = {
    'cle_presente': bool(key),
    'espaces_peripheriques': key != key.strip(),
    'retours_ligne': '\r' in key or '\n' in key,
    'guillemets_peripheriques': key.startswith(('"', "'")) or key.endswith(('"', "'")),
    'prefixe_bearer_saisi': key.lstrip().lower().startswith('bearer '),
    'espaces_dans_la_cle': any(c.isspace() for c in key),
}
summary = {
    'statut': 'STOP',
    'fournisseur': 'river',
    'modele': MODEL,
    'post_count': 0,
    'http_status': None,
    'json_schema_strict_accepte': None,
    'reasoning_effort_none_accepte': None,
    'max_tokens': 2048,
    'validation_pydantic': None,
    'resultat_negociation': None,
    'latence_ms': None,
    'usage': None,
    'auth_locale': auth,
    'erreur_origine': None,
    'erreur_controle': None,
}

async def main():
    if not key or any(value for name, value in auth.items() if name != 'cle_presente'):
        summary['erreur_origine'] = 'saisie_suspecte_ou_absente'
        summary['action'] = 'Nouvelle saisie requise ; aucune correction automatique.'
        return
    assert not Path('.env').exists()

    import app
    from app.core.config import settings
    from app.core.http_client import close_shared_http_client, get_shared_http_client
    from app.core.logging import JSONFormatter, redact_text
    from app.providers.llm.registry import build_llm_client, llm_client
    from app.services.ai.modules.negotiation import NEGOTIATION_SYSTEM_PROMPT
    from app.services.ai.schemas import NegotiationPayload

    assert Path(app.__file__).resolve().parent == Path(os.environ['BOTRADAR_SOURCE']) / 'app'
    assert settings.AI_ENABLED and settings.AI_PROVIDER == 'river'
    assert settings.AI_MODEL == MODEL and settings.AI_REASONING_EFFORT == 'none'
    assert settings.AI_BASE_URL == 'https://api.river.ai/v1'
    assert settings.AI_MAX_TOKENS == 2048 and settings.AI_TIMEOUT_SECONDS == 12.0
    assert settings.AI_API_KEY.get_secret_value() == key
    assert llm_client.api_key == key
    assert llm_client.chat_endpoint() == ENDPOINT
    fake = build_llm_client('river')
    fake.api_key = 'synthetic-auth-check'
    assert fake.chat_headers()['Authorization'] == 'Bearer synthetic-auth-check'
    auth['configuration_et_client_concordants'] = True
    auth['entete_factice_correct'] = True

    if os.environ.get('BOTRADAR_AUTH_LOCAL_ONLY') == '1':
        summary.update(statut='LOCAL_OK', action='Aucun appel réseau effectué.')
        return

    class Capture(logging.Handler):
        def emit(self, record):
            if getattr(record, 'event', None) == 'llm_request':
                records.append(json.loads(JSONFormatter().format(record)))

    async def observe_request(request):
        river_diagnostic['request_time'] = datetime.now(ZoneInfo('Europe/Paris')).isoformat()
        requests.append((request.method, str(request.url)))
        assert requests == [('POST', ENDPOINT)]
        headers = [value for name, value in request.headers.raw if name.lower() == b'authorization']
        assert len(headers) == 1 and headers[0].decode('ascii') == 'Bearer ' + key
        body = json.loads(request.content)
        assert body['model'] == MODEL
        assert body['max_tokens'] == 2048 and body['reasoning_effort'] == 'none'
        assert 'thinking' not in body and 'max_completion_tokens' not in body
        mode = body['response_format']
        assert mode['type'] == 'json_schema' and mode['json_schema']['strict'] is True
        assert mode['json_schema']['name'] == 'NegotiationPayload'
        assert mode['json_schema']['schema']['additionalProperties'] is False

    def local_text(value, limit=512):
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return None
        text = str(value)
        if len(text) > 4096:
            return '[OMITTED: oversized field]'
        if key and any(key[i:i + 4] in text for i in range(max(0, len(key) - 3))):
            return '[OMITTED: possible credential fragment]'
        text = redact_text(text.replace(key, '[REDACTED]'))
        text = re.sub(r'(?i)\b(?:rv_|sk[-_])[A-Za-z0-9_-]+', '[REDACTED]', text)
        return text[:limit]

    async def observe_response(response):
        river_diagnostic.update(
            endpoint=ENDPOINT,
            model=MODEL,
            http_status=response.status_code,
            x_request_id=local_text(response.headers.get('x-request-id'), 256),
        )
        challenge = response.headers.get('www-authenticate', '')
        scheme = challenge.split(None, 1)[0].lower() if challenge else None
        river_diagnostic['www_authenticate_scheme'] = (
            scheme if scheme in ('bearer', 'basic', 'digest') else ('other' if scheme else None)
        )
        auth_error = re.search(r'(?i)\berror="([^"\r\n]*)"', challenge)
        river_diagnostic['www_authenticate_error'] = (
            local_text(auth_error.group(1), 128) if auth_error else None
        )
        if response.is_error:
            await response.aread()
            if len(response.content) > 65536:
                river_diagnostic['body_parse'] = 'omitted_oversized'
                return
            try:
                body = response.json()
            except ValueError:
                river_diagnostic['body_parse'] = 'not_json'
                return
            error = body.get('error', body) if isinstance(body, dict) else None
            if isinstance(error, str):
                river_diagnostic['provider_message'] = local_text(error)
            elif isinstance(error, dict):
                for name in ('code', 'type', 'message'):
                    river_diagnostic['provider_' + name] = local_text(error.get(name))
                param = error.get('param')
                if param in {'model', 'response_format', 'reasoning_effort', 'max_tokens'}:
                    error_fields.append(param)
            river_diagnostic['body_parse'] = 'selected_fields_only'

    logger = logging.getLogger('app.providers.llm.base')
    capture = Capture()
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    client = get_shared_http_client()
    client.event_hooks['request'].append(observe_request)
    client.event_hooks['response'].append(observe_response)
    try:
        payload = await llm_client.generate_json(
            system_prompt=NEGOTIATION_SYSTEM_PROMPT,
            user_prompt='Annonce fictive : une BD en bon état à 20 euros. Propose une offre et un message français.',
            response_model=NegotiationPayload,
            ai_module='manual_preflight',
        )
        assert requests == [('POST', ENDPOINT)] and len(records) == 1
        record = records[0]
        assert record['provider'] == 'river' and record['model'] == MODEL
        assert record['attempt'] == 1
        if record.get('http_status') == 200:
            summary['json_schema_strict_accepte'] = True
            summary['reasoning_effort_none_accepte'] = True
        if record['status'] != 'ok' or payload is None:
            summary['erreur_origine'] = record['status']
            if record['status'] == 'validation_error':
                summary['validation_pydantic'] = False
            return
        result = NegotiationPayload.model_validate(payload)
        summary['validation_pydantic'] = True
        assert result.suggested_offer_cents is not None and result.message
        summary['resultat_negociation'] = {
            'offre_centimes': result.suggested_offer_cents,
            'message': redact_text(result.message.replace(key, '[REDACTED]')),
        }
        usage = record['usage']
        for name in ('input', 'output'):
            assert type(usage[name]) is int and usage[name] >= 0
        assert usage['output'] <= 2048
        for subset, total in (('cached', 'input'), ('reasoning', 'output')):
            count = usage[subset]
            assert count is None or (type(count) is int and 0 <= count <= usage[total])
        assert usage['reasoning'] in (None, 0)
        summary['statut'] = 'PASS'
    finally:
        try:
            await close_shared_http_client()
        finally:
            logger.removeHandler(capture)

try:
    asyncio.run(main())
except Exception as exc:
    summary['erreur_controle'] = type(exc).__name__
finally:
    summary['post_count'] = len(requests)
    if records:
        record = records[-1]
        summary.update(
            categorie_technique=record.get('status'),
            http_status=record.get('http_status'),
            latence_ms=record.get('duration_ms'),
            usage=record.get('usage'),
        )
    if error_fields:
        summary['parametre_rejete'] = error_fields
    if river_diagnostic.get('http_status') is not None:
        try:
            path = Path.cwd() / 'river-diagnostic-local.json'
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as output:
                json.dump(river_diagnostic, output, ensure_ascii=True, indent=2)
            summary['diagnostic_local_a_examiner'] = str(path)
            summary['partage_diagnostic'] = 'Ne pas copier ce fichier sans revue locale.'
        except Exception as exc:
            summary['diagnostic_local_erreur'] = type(exc).__name__
    print(json.dumps(summary, ensure_ascii=False, indent=2))

raise SystemExit(0 if summary['statut'] in ('PASS', 'LOCAL_OK') else 1)
PY_RIVER_PREFLIGHT
)
```

Un succès démontre l'accès à cet identifiant de modèle et l'acceptation de ces
paramètres avec ce seul schéma. Il ne prouve ni l'application universelle du schéma
par River, ni la qualité des cinq usages. Le plafond est vérifié dans la requête
et les compteurs observés ; sa bonne application dans tous les cas n'est pas
prouvée par une petite réponse. Les compteurs détaillés absents restent inconnus.
Un compteur de raisonnement positif avec `none` impose un examen avant la suite.
Les compteurs cachés et de raisonnement sont des sous-ensembles, jamais additionnés
aux totaux. Aucun prix n'est calculé. Ni prompt ni réponse métier brute n'est affiché.

En cas d'échec, conserver seulement le diagnostic structuré : catégorie, code HTTP,
modèle, compteurs et, si River le fournit sous cette forme, le paramètre rejeté
parmi les quatre noms autorisés. Ne jamais copier le corps brut d'erreur. Le format
HTTP des erreurs n'étant pas exhaustivement documenté, un code 400/422 seul ne
permet pas de désigner le paramètre fautif. Si la preuve manque, noter « cause non
établie », sans inventer de diagnostic. Examiner le paramètre ou schéma incriminé
au regard des sources River ci-dessus et proposer la plus petite correction avant
un nouveau mandat. Aucun second POST, changement automatique de mode JSON ou de
fournisseur, ni retrait de validation métier. Un timeout peut avoir été facturé
côté serveur ; ne pas rejouer pour obtenir un diagnostic plus commode.

### Ensuite seulement : une passe qualité existante

Cette commande distincte nécessite la réussite du premier test et une nouvelle
autorisation explicite pour dix appels River au maximum et les accès catalogue.
Elle nécessite un nouveau shell dédié avec la même configuration River, les cinq
flags de modules actifs, la clé injectée de façon sécurisée et un répertoire
courant neuf sans `.env`. Le sous-shell du contrôle précédent ne conserve aucune
clé pour cette étape. Ne pas chaîner les deux étapes, ne pas exécuter la sonde de
latence (deux appels supplémentaires).

```sh
"$BOTRADAR_PYTHON" -B "$BOTRADAR_SOURCE/scripts/probe_llm_quality.py" --passes 1
```

Une passe représente trois extractions `/add`, deux variantes, deux négociations,
deux scorings et un auto-tuning, soit dix complétions au maximum. Le défaut de
trois passes ferait trente appels et ne fait pas partie du protocole. Les accès
bibliographiques de `/add` sont des requêtes supplémentaires, distinctes des appels
LLM et à autoriser séparément. Le budget cumulé des deux étapes est **onze POST
River au maximum**, sans aucun appel OpenAI. Tout nouveau passage réclame un mandat.

La sonde présente les résultats métier et les diagnostics par cas. Examiner chacun
des dix résultats : fidélité de `/add`, ISBN jamais inventé comme autorité, exclusions
sans rejet abusif, français et offre adaptés, chiffres cohérents et conseil
consultatif, auto-tuning pertinent sans application automatique. Un repli déterministe,
un `None` ou une sortie rejetée n'est jamais une réussite du modèle. Une liste vide
pertinente peut être un avis valide, à examiner individuellement. Le code de sortie
mécanique ne vaut pas approbation qualitative. Rapporter erreurs, latences et
consommations, sans garantie statistique de production ni validation de cote réelle.

### Vérification locale de cette procédure

L'ajout de collecte fournisseur a fait l'objet d'une vérification ponctuelle sur
deux objets `httpx.Response` factices, sans client HTTP ni requête : extraction des
champs sélectionnés et exclusion des cookies/corps annexes ; omission de champs
contenant une clé factice ou un fragment. Une première valeur d'identifiant factice
coïncidait avec un fragment de la clé factice et a été masquée comme prévu ; la
vérification a ensuite utilisé un identifiant sans cette coïncidence. Aucun test
applicatif supplémentaire n'a été ajouté, aucun contrôle distant exécuté. Les
27 tests précédents décrivent la version avant cet ajout, sans attester la réponse
réelle de River ni être présentés comme une nouvelle preuve de sa cause de refus.


Le diagnostic d'authentification et le bilan révisé, avant ajout de la collecte
de réponse fournisseur, avaient ensuite été vérifiés sur
**27 tests hors réseau réussis en 1,42 s**, sortie 0 : saisies suspectes rejetées
sans correction, transmission par l'environnement vers `Settings` et la factory,
entête factice, mode local sans client HTTP, erreurs 401/403 et autres échecs avec
un seul POST simulé, champs non vérifiés à `null`, séparation erreur HTTP/contrôle,
expurgation et conservation de la saisie sous zsh. Le code applicatif est inchangé.

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target /private/tmp/bot-radar-auth-check/test_river_auth_doc.py
```

Le test temporaire est hors inventaire livré. Une première collecte depuis `/tmp`
a échoué avant exécution (sortie 4, accès interdit à un répertoire voisin) ; la
collecte a réussi dans un répertoire temporaire dédié avec configuration pytest
locale. Aucun assouplissement de l'isolation ni accès distant n'a été ajouté.
La syntaxe zsh/Python des blocs et les cibles des liens locaux ont été vérifiées.


La version initiale de la commande du premier test avait été exécutée uniquement
avec le vrai client et `httpx.MockTransport` sous le lanceur isolé : **10 tests
réussis en 1,64 s**, sortie 0. Les scénarios couvrent succès, HTTP 400/422/429,
timeout, contenu vide, JSON invalide, validation Pydantic, consommation absente et
raisonnement positif inattendu. Chaque scénario observe exactement un POST simulé,
la fermeture du client et aucune sentinelle sensible dans la sortie.

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target /tmp/test_river_manual_doc.py
```

Ce fichier temporaire est une vérification ponctuelle, hors inventaire livré.
Ces doubles ne prouvent aucune capacité réelle de River. Le lanceur rapporte
`allowed=0` ; ses refus réseau et dotenv sont les sondes attendues d'isolation.
Seul ce rapport a changé pendant la préparation. Les résultats Ruff, mypy et suite
complète ci-dessous concernent le code applicatif inchangé de la livraison River ;
ils n'ont pas été rejoués pour cette modification documentaire.

## Bascule et observation en production

La procédure suivante est documentaire ; son exécution nécessite le mandat
opérationnel et la procédure de [déploiement](deployment.md).

1. Disposer du code validé et des résultats de qualité hors production examinés.
2. Conserver les anciennes variables via le dispositif sécurisé de l'exploitant,
   sans export ni affichage de secrets dans les preuves.
3. Définir le bloc River et sa clé dans l'environnement du service.
4. Recréer le seul service applicatif pour recharger les variables. Un simple
   `restart` Compose ne recharge pas `env_file`. Ne pas toucher aux volumes ou
   services de données pour ce changement de fournisseur. Après préparation de
   l'image validée selon la procédure de déploiement, depuis le dossier Compose
   d'exploitation, la commande prévue est :

   ```sh
   docker compose up -d --no-deps --no-build --pull never --force-recreate app
   ```

   Vérifier avant/après l'identité et le démarrage des conteneurs PostgreSQL/Redis
   selon la procédure canonique. Ne pas utiliser cette commande avec une ancienne
   image ne contenant pas l'adaptateur River. Elle n'a pas été exécutée ici.
5. Consulter `GET /health` sur l'interface locale puis envoyer `/status` dans
   Telegram depuis le compte autorisé. `/status` est une commande du bot, pas
   une route HTTP. Examiner le bloc IA ; un HTTP 200 de `/health` ne valide ni
   génération, ni disponibilité du modèle, ni capacité métier complète. Ces
   consultations peuvent déclencher la sonde GET `/models`, mise en cache 30 s,
   sans complétion. Vérifier aussi la continuité des alertes Telegram.
6. Observer les cinq usages disponibles, les accusés Telegram et les diagnostics
   `llm_request` : fournisseur `river`, modèle exact avec suffixe `1017`, tentative 1,
   statuts, durée, consommation. Vérifier l'acceptation du schéma strict et de
   `reasoning_effort=none` ; un 400/422 ne déclenche aucune autre requête.
7. En cas de refus d'accès, schéma rejeté, troncatures récurrentes ou résultat
   métier inacceptable, effectuer le rollback. Ne pas activer un moteur secondaire.

Pendant les premiers jours, examiner chaque jour les événements `llm_request` :
répartition des statuts HTTP, tout 401, fréquence des 429, timeouts, latences
médiane et haute, consommation `usage.input`/`output` et leurs sous-ensembles
`cached`/`reasoning`. Ne pas sommer ces sous-ensembles aux totaux. Suivre aussi
`empty_content`, `invalid_json`, `validation_error`, `invalid_envelope`, `refused`
et `interrupted`. Les événements `ai_scoring` distinguent `abstained`, `estimated`
et `incomplete` : une abstention motivée n'est pas une panne ; un taux croissant
demande un examen des entrées et de leur identification. Ne pas conserver de
prompts ou réponses complets dans les journaux. Aucun seuil statistique fiable
ne découle de cette petite passe. Aucun appel synthétique payant automatique.

## Rollback par configuration

Restaurer ensemble les variables réellement utilisées avant la bascule, conservées
par l'exploitant : fournisseur, modèle, URL, clé, budgets et flags. Ne pas supposer
que les défauts du dépôt décrivent la production. Le bloc Luna ci-dessus est une
configuration future disponible, pas un fallback automatique ni une observation
de l'ancienne production. L'adaptateur DeepSeek officiel reste aussi disponible
pour une configuration précédente qui l'utilisait.

Recréer le seul service applicatif avec la même commande `--no-deps --no-build
--pull never --force-recreate app` pour recharger ses variables, puis vérifier
santé, usage et continuité des alertes. Le rollback change le fournisseur avec le
nouveau code et ses validations, sans restauration des anciens défauts de parsing.
Aucune migration de données ni modification métier nécessaire. La compatibilité
est couverte par doubles ; l'accès réel des comptes et le rollback distant restent
non exécutés. Une panne River ne provoque jamais cette bascule d'elle-même.

## Risques et recommandations limitées

- River : compatibilité technique observée sur les dix appels, qualité inégale.
  Les prompts corrigés restent à revalider. Accès réel à Luna non observé.
  Les corps d'erreur HTTP ne sont pas spécifiés exhaustivement ; limites ci-dessus.
- Plafond 2 048 et délai 12 secondes : à confirmer sur les cas représentatifs ; ne
  pas augmenter le budget pour cacher des troncatures ou une sortie inadaptée.
- L'estimation IA n'observe pas le marché en direct ; les tests ne prouvent aucune
  cote réelle et ne changent pas les règles de prix ou l'autorité du catalogue.
- Préserver le test « un seul POST » et les cas de qualité lors du prochain
  changement de modèle. Aucun système de quota ni infrastructure d'évaluation supplémentaire proposé.

## Adaptations de l’étape River, avant les corrections de qualité

Le recomptage à la fin de l’étape River, avant les corrections de qualité, distinguait :

| Périmètre | Fichiers déjà suivis modifiés | Nouveaux fichiers | Total |
| --- | --- | --- | --- |
| Migration initiale Luna | 31 | 5 | 36 |
| Diff cumulé après River | 31 | 7 | 38 |

L'étape River concerne **13 fichiers : 11 adaptations parmi les 36 fichiers
initiaux et 2 ajouts**. Les 25 autres fichiers initiaux étaient alors identiques octet pour
octet. Les 11 adaptations incluent des fichiers créés pendant la migration Luna ;
elle ne signifie donc pas 11 fichiers déjà suivis par Git. Le tableau ci-dessous
contient bien ces 13 fichiers, avec les deux ajouts explicitement signalés.

Le périmètre courant est recalculé depuis `git diff --name-only HEAD` et
`git ls-files --others --exclude-standard`, en dédupliquant les chemins. L'état
intermédiaire non commité n'est pas reconstructible depuis Git seul : le détail
36 puis 11 + 2 repose aussi sur l'inventaire et les empreintes sauvegardés avant
River, rapprochés du périmètre Git actuel. Il n'existe pas de commit intermédiaire.
Cette préparation manuelle ne modifie que le présent rapport, déjà dans le périmètre.
Aucun travail étranger, reset, checkout, nettoyage ou reconstruction de branche.

| Fichier | Raison de l'adaptation River |
| --- | --- |
| `app/providers/llm/river.py` (ajout) | Identité d'inférence `river`, traduction du budget en `max_tokens`, réutilisation du protocole structuré existant |
| `app/providers/llm/openai.py` | Nom du champ de budget porté par l'adaptateur ; requête Luna inchangée |
| `app/providers/llm/registry.py` | Enregistrer River dans la factory unique, sans fallback ni second client actif |
| `app/core/config.py` | Défauts River, URL officielle et identifiant exact `1017` ; variables existantes réutilisées |
| `.env.example` | Même cible et même clé unique, vide dans le fichier d'exemple |
| `README.md` | Cible courante River ; Luna disponible mais inactive |
| `docs/contracts/telegram-ai.md` | Protocole River, sources et limites, compteurs sans double comptage, santé et absence de retry |
| `docs/operations/llm-migration.md` | Deux blocs de configuration, vérification manuelle, bascule/retour, rapport et limites de preuve |
| `tests/unit/test_llm_river.py` (ajout) | Contrat exact HTTP River, compteurs, santé, erreurs, secret, un POST et repli sans OpenAI |
| `tests/unit/test_llm_contract.py` | Exécuter le contrat commun aussi sur River, sans retirer les autres adaptateurs |
| `tests/unit/test_llm_registry.py` | Vérifier les cinq adaptateurs enregistrés |
| `tests/unit/test_llm_migration.py` | Comparaisons de réponses aussi sur River ; renforcer l'assertion explicite du modèle/endpoint Luna |
| `tests/unit/test_delivery_outcomes.py` | Le défaut vérifie River ; ajouter les mêmes scénarios sur OpenAI pour conserver la couverture future |

Tous les modules métier, prompts, schémas, traitements d'erreur, compteurs,
replis, corrections précédentes et sondes de qualité sont conservés. Le mapping
River réutilise exactement le parseur de compteurs existant ; aucune estimation
financière n'est ajoutée. Aucune nouvelle dépendance ni variable d'environnement.

## Inventaire exact du diff cumulé après corrections de qualité

Le diff inclut la migration initiale, l’adaptation River et les corrections de
qualité du 25 octobre : **33 fichiers modifiés, 9 ajoutés, aucun supprimé (42 au
total)**. Fichiers modifiés :

- `.env.example`
- `README.md`
- `app/core/config.py`
- `app/providers/llm/anthropic.py`
- `app/providers/llm/base.py`
- `app/providers/llm/openai_compatible.py`
- `app/providers/llm/registry.py`
- `app/services/ai/modules/auto_tuning.py`
- `app/services/ai/modules/keyword_variants.py`
- `app/services/ai/modules/natural_add.py`
- `app/services/ai/modules/negotiation.py`
- `app/services/ai/modules/profit_scoring.py`
- `app/services/ai/pipeline.py`
- `app/services/ai/schemas.py`
- `docs/contracts/telegram-ai.md`
- `scripts/probe_llm_latency.py`
- `scripts/probe_llm_quality.py`
- `tests/add_fakes.py`
- `tests/fakes_llm.py`
- `tests/unit/test_add_creation_card.py`
- `tests/unit/test_add_direct_recap.py`
- `tests/unit/test_add_isbn.py`
- `tests/unit/test_add_keyboards.py`
- `tests/unit/test_add_keyword_analysis.py`
- `tests/unit/test_add_screens.py`
- `tests/unit/test_autotune_and_variants.py`
- `tests/unit/test_ai_subsystem.py`
- `tests/unit/test_charte_copy.py`
- `tests/unit/test_delivery_outcomes.py`
- `tests/unit/test_llm_contract.py`
- `tests/unit/test_llm_registry.py`
- `tests/unit/test_probe_llm_quality.py`
- `tests/unit/test_reliability_regressions.py`

Fichiers ajoutés :

- `app/providers/llm/deepseek.py`
- `app/providers/llm/openai.py`
- `app/providers/llm/river.py`
- `docs/operations/llm-migration.md`
- `tests/unit/test_llm_migration.py`
- `tests/unit/test_llm_river.py`
- `tests/unit/test_probe_llm_latency.py`
- `tests/unit/test_ai_quality_regressions.py`
- `tests/fixtures/ai_quality_observed.json`

Fichiers supprimés : aucun. Index vide ; aucun commit ni push.

## Bilan historique River, avant les essais réels et les corrections de qualité

À la fin de l'adaptation River, avant les essais réels : statut technique
**terminé et validé localement**, statut opérationnel **partiel** ; qualité réelle
et bascule alors non exécutées. Base et HEAD :
`9fe331ee54fe920baf23cc0c472b38fc88374d03`, périmètre historique non committé de
31 fichiers modifiés et 7 ajoutés, aucun supprimé, index vide. L'inventaire courant
ci-dessus inclut les corrections suivantes et compte désormais 42 fichiers.

Environnement : Python 3.12.0, Pydantic 2.13.5, HTTPX 0.28.1. Interpréteur réutilisé :
`/Users/aminezegmou/Projets/bot-radar/.venv/bin/python`. La provenance de `app` a été
vérifiée dans le worktree de migration ; aucune installation ni dépendance ajoutée.

La première livraison Luna avait passé 3 358 tests au même SHA de base. Cette
preuve historique n'est pas utilisée pour attester le nouveau défaut River :
la suite complète ci-dessous a été rejouée après adaptation.

Commandes finales exécutées depuis le worktree de migration :

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check . --no-cache
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
git diff --check
git diff --cached --stat
```

| Contrôle | Résultat exact |
| --- | --- |
| Ruff complet | `All checks passed!`, sortie 0 |
| Typage isolé principal | `Success: no issues found in 172 source files`, sortie 0 |
| Typage isolé supplémentaire | `Success: no issues found in 8 source files`, même exécution sortie 0 |
| Suite complète isolée | **3 424 passed, 124 skipped, 4 warnings**, 91,94 s, sortie 0 |
| Rapport du lanceur | 3 548 tests collectés, aucun échec, aucun service réel autorisé |
| Isolation finale | Configuration factice ; deux refus réseau attendus de la sonde ; `allowed=0`, `process_denied=0`, `postgres_config=0`, `service_denied=0` |
| Diff et index | Pas d'erreur d'espacement ; index vide |
| Liens locaux des trois documents modifiés/ajoutés | Cibles existantes |
| Conservation des tests | Aucune fonction de test de la base retirée ou renommée dans le diff |

Le nouveau contrat River a été reproduit en échec avant ajout de l'adaptateur.
La sélection fournisseur/validation/livraison a ensuite passé **330 tests** ;
la sélection renforcée River/Luna/Telegram a passé **157 tests**, sans réseau.
Le contrat commun reste exécuté sur les cinq adaptateurs. Les erreurs River
402, 404, 413, 422, 429 et les autres statuts testés conservent un seul POST et
leur repli, sans sonde, second mode ni fournisseur de secours. Le mapping des
sous-compteurs est testé après sérialisation JSON, sans clé ni contenu externe.

Les tests ciblés de la migration initiale avaient aussi été exécutés, avec reproduction puis
correction des listes non textuelles, du rationale journalisé, des constantes JSON
non standard et des replis comptés comme succès dans la sonde. Les tests de
caractérisation des réponses vides passaient avant modification et restent verts.

Les quatre avertissements ont été reproduits sur `main` inchangée au même SHA :
41 tests ciblés réussis avec les trois avertissements FastAPI 422/Alembic ;
15 tests de sécurité réussis avec l'avertissement Starlette/TestClient HTTPX.
Ils préexistent au diff. Aucune dépendance ni configuration hors IA n'a été modifiée
pour les masquer. Les 124 tests ignorés restent explicitement non attestés.

Le manifeste historique généré `docs/consolidation-nodeids.json` n'est pas un
inventaire à jour de cette base : 18 références n'y correspondent déjà plus
(13 fonctions absentes de HEAD, 5 anciens identifiants de paramétrage). Aucun de ces
écarts n'est introduit ici ; aucun fichier concerné par ces changements historiques
n'est modifié pour les corriger. Le manifeste n'est ni réécrit ni présenté comme
validé. La suite complète prescrite et la conservation des fonctions de test du
diff sont vérifiées séparément.

L'auto-revue couvre le diff applicatif, les nouveaux adaptateurs, les consommateurs,
les diagnostics sérialisés, les références et l'index ; ce n'est pas une revue
indépendante. Le dossier principal est resté propre sur `main` au SHA de base.
Aucun secret lu ou exposé, aucun `.env` réel modifié, aucun appel IA payant, commit,
push ou déploiement effectué. La production et les comptes fournisseurs ne sont
pas attestés par ces résultats locaux.


## Corrections de qualité du 25 octobre 2026

### Preuves et diagnostic avant modification

Le rapport local `quality-report.json` de la passe de 00:47:44 à 00:48:22
Europe/Paris (UTC+02:00) était disponible. Il contient dix réponses HTTP 200,
validées par Pydantic, `finish_reason=stop`, tentative 1 et zéro token de raisonnement.
La sonde mécanique avait neuf succès et un échec ; l'examen manuel avait cinq
réussites, trois cas douteux et deux échecs. Ce résultat n'est pas réinterprété comme
une validation de production.

Les dix objets JSON observés et les entrées exactes des cas sont conservés dans
`tests/fixtures/ai_quality_observed.json`, sans clé ni enveloppe HTTP brute. Les
entrées viennent des cas de la sonde relus, pas d'une prétendue récupération des
requêtes HTTP complètes. Les réponses restent les observations initiales : elles
n'ont pas été remplacées par des réponses améliorées. Les états catalogue utilisés
par les tests de régression sont des doubles explicites, pas des preuves distantes.

| Problème | Diagnostic et niveau de preuve |
| --- | --- |
| Absolute Martian Manhunter | Le modèle a proposé `false`, puis le résultat était `book_unidentified=True`, sans source ni ISBN. Le rapport ne conserve aucune notice prouvant une série pendant cet appel. Le défaut observable est une classification non résolue, pas une œuvre unique confirmée. |
| Attente `true` du cas | Benchmark historique appuyé sur les notices numérotées des tests `test_series_detection.py` et sur la régression d'un ISBN de tome attaché à une veille de série. Ces fixtures prouvent la règle du code, pas la réponse actuelle du catalogue. L'attente reste un objectif de qualité ; le rattrapage catalogue n'est pas garanti et son absence ne prouve aucune nature. |
| Variantes Absolute | Cinq copies du titre original et un pluriel, toutes retirées. Erreur de suivi des consignes par le modèle ; la sonde masquait le problème en ne jugeant que la liste nettoyée. |
| Variantes Blacksad | Une variante utile et des répétitions après normalisation. Même limite d'observation de la sonde. Les exclusions génériques ne sont pas automatiquement mauvaises : leur pertinence reste à examiner sur le contexte et les annonces. |
| Scoring des deux annonces vagues | Identification insuffisante normale avec les données fournies ; le prompt demandait pourtant des montants et la sonde exigeait des chiffres. Ambiguïté du contrat et attente de test incorrecte, en plus de prix non étayés dans les sorties observées. |
| Coût absent et marge | Défauts de code reproduits : coût source sans frais laissé à zéro dans le prompt ou inconnu au parsing, score partiel affichable, marge incohérente acceptée. La formule revente moins achat était incorrectement appelée nette. |

Aucune règle « sans tome = série », exception de titre ou identité inventée n'a été
introduite. `is_series`, `book_unidentified`, `requested_volume` et `work_choices`
suffisent à préserver les états existants ; aucun nouveau schéma de classification
n'est nécessaire pour cette correction. Le prompt explicite leur portée et le
respect de l'incertitude. Le code de résolution et les filtres du domaine sont conservés.

### Changements et attentes conservées ou corrigées

- Variantes : une proposition vide est explicitement autorisée, sans quota. Les
  différences de casse/accents/ponctuation ou espaces superflus ne sont pas des
  variantes à remplir ; la décision de normalisation reste au code. Le résultat
  conserve les propositions et `SanitizedValues`, déjà fourni par le filtre existant.
- Sonde : répétitions normalisées et listes entièrement filtrées ne passent plus
  silencieusement pour une qualité satisfaisante. Une liste partiellement filtrée
  reste distinguée et à examiner ; une liste vide sans proposition reste conforme,
  sans prétendre prouver sa pertinence sémantique.
- Scoring : abstention avec les trois champs numériques existants à `null` et une
  raison dans `verdict`. Aucun champ ni type filaire ajouté. Cette abstention valide
  affiche une explication en arrière-plan, sans prix de revente, marge ni score.
- Coût/marge : coût inconnu non remplacé par zéro, prix source sans frais réutilisé
  lorsqu'il est connu, marge calculée localement et réponse incohérente refusée.
  Le nom historique `net_profit_cents` est conservé, avec affichage de marge brute
  estimée avant frais de revente. Aucun calcul de frais ou formule marché modifié.
- Les deux cas de scoring vagues exigent désormais une abstention motivée. L'ancienne
  réponse numérique observée ne passe pas ce nouveau critère. Le test d'arithmétique
  conserve ses assertions sur marge incohérente puis cohérente, dans un cas d'article
  identifié ; il n'est pas supprimé. Le test d'affichage garde le même montant et
  contrôle le libellé corrigé. Aucun nodeid historique supprimé.
- Le contrat d'un POST, les cinq adaptateurs et leurs paramètres sont inchangés.
  Le scénario d'abstention est aussi testé sur chacun des adaptateurs existants,
  notamment celui conservé pour Luna. Aucune dépendance ajoutée.

Fichiers concernés par cette étape :

| Fichier | Changement |
| --- | --- |
| `app/services/ai/modules/natural_add.py` | Clarification du périmètre de surveillance et de l'incertitude dans le prompt uniquement |
| `app/services/ai/modules/keyword_variants.py` | Liste vide explicite et diagnostics du filtre existant dans le résultat |
| `app/services/ai/modules/profit_scoring.py` | Abstention, coût connu/inconnu, cohérence de marge, affichage et diagnostics sans texte sensible |
| `app/services/ai/schemas.py` | Propriété locale d'abstention et description correcte de la marge, sans nouveau champ filaire |
| `scripts/probe_llm_quality.py` | Qualité brute des variantes, abstention attendue, sélection d'usages et comptage avant appel |
| `tests/fixtures/ai_quality_observed.json` (ajout) | Dix réponses et entrées expurgées de la passe réelle |
| `tests/unit/test_ai_quality_regressions.py` (ajout) | Reproduction et régressions locales des problèmes observés |
| `tests/unit/test_ai_subsystem.py` | Libellé de marge brute, même calcul attendu |
| `tests/unit/test_probe_llm_quality.py` | Conserver le contrôle arithmétique dans un contexte identifié |
| `tests/unit/test_llm_migration.py` | Abstention nullable sur les cinq adaptateurs |
| `docs/contracts/telegram-ai.md` | Contrat des scopes, des variantes et du scoring |
| `docs/operations/llm-migration.md` | Présent diagnostic, périmètres datés et revalidation préparée |

Aucun fichier supprimé. Par rapport aux 38 fichiers de l'étape River, cette étape
ajoute deux chemins jusque-là inchangés (`profit_scoring.py`, `test_ai_subsystem.py`)
et deux nouveaux fichiers, soit 42 chemins cumulés. La factory, les adaptateurs,
la configuration, les transports et les dépendances sont inchangés pendant cette étape.

### Revalidation réelle minimale préparée, non exécutée

Nouvelle autorisation et nouvelle clé nécessaires. Exécuter depuis un répertoire
courant dédié sans `.env`, avec le code du worktree dans `PYTHONPATH`, le bloc de
configuration River existant et les trois flags concernés actifs. Ne pas importer
l'application depuis le répertoire contenant un `.env` réel. Commande indicative,
à ne pas lancer dans les tests automatisés :

```sh
"$BOTRADAR_PYTHON" -B "$BOTRADAR_SOURCE/scripts/probe_llm_quality.py" \
  --passes 1 \
  --module natural_add \
  --module keyword_variants \
  --module profit_scoring
```

**Sept POST au maximum : trois `/add`, deux variantes/exclusions, deux scorings**,
un par cas, sans retry ou fallback. La sonde affiche ce comptage avant les appels.
Les négociations et l'auto-tuning inchangés ne sont pas rappelés. Les `/add` peuvent
consulter BNF/OpenLibrary et Google Books seulement si ce dernier est configuré ;
ces lectures doivent être signalées et autorisées avec la nouvelle passe. Aucun
accès catalogue réel n'a été effectué pendant les présentes corrections locales.

Critères : distinguer veille de série, volume précis, œuvre unique et identité
non résolue/ambiguë sans ISBN inventé ; constater propositions initiales, retraits
et listes réellement utiles ; examiner les exclusions dans leur contexte, sans
les interdire par vocabulaire ; exiger des deux scorings vagues une abstention avec
raison et trois champs numériques nuls. Conserver HTTP, catégorie, Pydantic,
troncature, latence, consommation et résultat individuel. Un repli ou une sortie
entièrement filtrée n'est pas une réussite qualitative automatique.

Les tests locaux établissent le comportement du code sur des réponses figées. Ils
ne démontrent pas que le modèle suivra le prompt corrigé, que le catalogue répondra,
ni que les exclusions retenues sont optimales. La qualité en production reste non attestée.


### Vérification finale des corrections locales

Base locale et HEAD : `9fe331ee54fe920baf23cc0c472b38fc88374d03`, branche
`feat/migration-gpt6-luna`, diff non committé, index vide. Douze fichiers concernés
par cette étape, dans un périmètre cumulé de 42 fichiers (33 modifiés, 9 ajoutés).
Aucun fichier supprimé. Les empreintes avant/après confirment que tous les fichiers
de `app/providers/llm/`, la configuration, `.env.example`, les dépendances et les
règles de détection de série/filtrage des mots-clés sont inchangés pendant cette étape.

Commandes exécutées depuis le worktree, avec le venv existant et le lanceur isolé :

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check . --no-cache
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
git diff --check
git diff --cached --stat
```

| Contrôle | Résultat |
| --- | --- |
| Reproduction initiale | 14 échecs attendus, 10 comportements déjà conformes ; 24 tests, 0,66 s |
| Sélection ciblée IA/contrats/Telegram/séries/hygiène | 500 passed, 7,84 s, sortie 0 |
| Ruff complet final | `All checks passed!`, sortie 0 |
| Typage isolé final | 172 fichiers principaux et 8 scripts sans erreur, sortie 0 |
| Suite complète finale | **3 462 passed, 124 skipped, 4 warnings**, 96,30 s, sortie 0 |
| Rapport du lanceur | 3 586 tests collectés, zéro échec, `allowed=0` |
| Sonde et régressions après mise en forme finale | 51 passed, 0,57 s, sortie 0 |
| Diff, liens locaux et index | Vérifiés ; index vide |

Le retour à la ligne final du message de configuration de la sonde a été vérifié
sans changement d'AST par rapport à la suite complète, puis ses 51 consommateurs
ont été rejoués. Le typage et Ruff finaux ont été rejoués après cette mise en forme.
Le commentaire préexistant du seuil d’affichage a aussi été conservé, avec preuve
d’AST identique ; Ruff a été revérifié après sa restauration. Le premier Ruff avait
signalé la ligne trop longue ; un essai de typage sans
accès au cache du worktree a aussi échoué avant analyse, puis réussi avec cet accès.
Ces essais ne sont pas présentés comme des contrôles verts.

Les quatre avertissements restent ceux déjà documentés : Starlette/TestClient HTTPX,
deux usages FastAPI 422 et configuration Alembic. Aucun changement de dépendance
pour les masquer. Les 124 tests ignorés ne prouvent aucun service PostgreSQL réel.
Les refus réseau/dotenv du lanceur sont ses sondes attendues ; aucun service réel
ni appel API payant n'a été autorisé ou exécuté pendant ces corrections.

Les tests historiques conservent leurs noms. Les deux attentes modifiées sont
justifiées ci-dessus : libellé de marge brute et contexte identifié du test
arithmétique. Aucun test utile supprimé, aucun repli requalifié en succès modèle.
L'auto-revue porte sur les corrections, leurs consommateurs, les tests, la
conservation du fournisseur et l'index ; elle n'est pas une revue indépendante.

**Bilan historique avant la revalidation : corrections locales vérifiées.** Leur
qualité réelle restait alors à revalider par sept appels. Aucun secret lu,
aucun `.env` réel consulté ou modifié, aucun appel distant, commit, push ou déploiement
pendant cette étape. Les essais payants précédents restent des preuves historiques
sur les anciens prompts, jamais des preuves du suivi des nouveaux prompts.

La dernière lecture de l'état du dossier principal signale une modification
étrangère de `.env.example` sur `main`. Elle n'a été ni lue ni modifiée par cette
étape ; les vérifications et empreintes ci-dessus portent sur le worktree de
migration, dont le `.env.example` reste inchangé par ces corrections.

## Revalidation acceptée et dernier alignement local du 25 octobre 2026

La passe ciblée autorisée a été exécutée de 10:14:11 à 10:14:30 Europe/Paris
(UTC+01), avant ce dernier ajustement. Sept POST River, tous HTTP 200, catégorie
`ok`, validation Pydantic réussie et `finish_reason=stop`, sans retry ni fallback.
La sonde a terminé avec le code 1 : six assertions de cas réussies, un échec sur
les variantes d'Absolute. Les deux scorings se sont abstenus avec une raison et
sans estimation inventée. L'extraction Absolute reste douteuse : la surveillance
de série provient ensuite du catalogue, pas d'une amélioration prouvée du modèle.
La qualité globale reste partielle ; cet échantillon ne prouve pas la production.

La seule modification applicative postérieure remplace `"variants": ["variante1"]`
par `"variants": []` dans l'exemple final du prompt. L'exemple des exclusions,
les autres instructions, schémas, filtres et adaptateurs sont inchangés. Le test
`test_variant_prompt_example_matches_permission_to_propose_no_variants` lit le
JSON final avec `KeywordVariantsPayload` et vérifie son accord avec l'autorisation
de liste vide et l'absence de quota. Il a échoué sur l'ancien exemple avant le
correctif. Ce test prouve la cohérence du texte, pas son respect par le modèle.
Aucune nouvelle passe payante prévue ou exécutée pour cet ajustement.

### Volume catalogue : observation et protections actuelles

Comptage hors réseau depuis le rapport expurgé de cette passe :

| Cas `/add` | BNF | OpenLibrary | Dont redirections OpenLibrary | Total HTTP |
| --- | ---: | ---: | ---: | ---: |
| Absolute Martian Manhunter | 3 | 15 | 6 | 18 |
| One Piece tome 12 | 3 | 3 | 1 | 6 |
| Le Monde sans fin | 1 | 0 | 0 | 1 |
| Total | 7 | 18 | 7 | 25 |

Les sept réponses BNF sont HTTP 200 ; OpenLibrary renvoie onze HTTP 200 et sept
HTTP 302. Les redirections sont comprises dans les 25 requêtes, pas ajoutées à
ce total. Aucun Google Books. Le rapport local temporaire est la preuve de ce
comptage, pas une nouvelle mesure réseau.

Le chemin est `NaturalAddModule._resolve_work` → `BookSearchUseCase.search_by_title`
→ `BookMetadataService.resolve_for_alert_report`, puis éventuellement
`lookup_series_notices(for_creation=True)`. Lecture directe du code :

- Aucun limiteur de débit, espacement, cache de réponses bibliographiques ou
  déduplication globale des consultations dans ce chemin. Le limiteur Redis de
  sécurité n'y est pas appelé. Le pool HTTP partagé (50 connexions, 20 conservées,
  keep-alive 30 s) réutilise des connexions ; ce n'est ni un quota ni un cache.
- Timeout HTTP de 8 s par requête catalogue par défaut. Il s'applique aux phases
  HTTPX, pas à toute la résolution ni à la chaîne de redirections. Pas d'échéance
  globale autour de la résolution dans le module. Les éditions OpenLibrary suivent
  explicitement les redirections ; HTTPX installé borne chaque chaîne à 20 sauts.
- Les recherches BNF demandent 10 notices par page ; en création, la pagination
  s'arrête quand le prochain début dépasse 100. Ce plafond de position n'est pas
  un budget global d'appels. La recherche OpenLibrary demande 10 résultats, mais
  son parseur ne tronque pas localement une réponse qui en fournirait davantage.
- Les 12 tomes usuels et 3 éditions alternatives sont des limites métier, pas des
  limites réseau. En création, la collecte OpenLibrary peut examiner jusqu'à 100
  candidats d'édition, consulter leurs notices exactes, puis vérifier leurs ISBN
  auprès des autorités. Une résolution d'un tome explicite peut également lire
  plusieurs notices d'édition. Plusieurs requêtes différentes peuvent donc suivre
  un seul appel IA. Les contrôles locaux ne constituent pas un plafond global
  fiable et explicite de requêtes HTTP par `/add`.

**Proposition séparée, non implémentée :** un budget partagé de 12 requêtes HTTP
catalogue par `/add`, redirections comprises et toutes autorités confondues, avec
une échéance monotone de 20 s pour toute la résolution bibliographique. Réserver
la priorité à l'identification de base et à l'ISBN explicitement demandé avant
l'expansion de série. À épuisement, rendre les informations partielles comme
partielles/non confirmées, sans nouvel appel ni identité inventée. Les 12 appels
et 20 s sont des choix proposés pour BotRadar, pas des limites publiées par les
catalogues. Le délai ne comprendrait pas la complétion IA.

Impact : le parcours Absolute observé à 18 requêtes serait interrompu au plus
tard à 12 et pourrait proposer moins de tomes ; les parcours à 6 et 1 resteraient
sous le budget de nombre, sans garantie de délai. Une borne par commande ne
limite pas à elle seule le débit de plusieurs commandes simultanées. Un éventuel
espacement partagé par autorité devra être traité dans ce lot distinct ; aucun
cache ou régulateur nouveau n'est ajouté ici. Ces protections ne changeraient ni
l'autorité bibliographique ni le chemin d'envoi des alertes.

La procédure de bascule et de rollback ci-dessus est prête à être examinée ; elle
reste non exécutée et nécessite un mandat opérationnel distinct. Le rollback
restaure l'ensemble du bloc de configuration précédent et sa clé sécurisée, puis
recrée uniquement `app`, sans migration de données. Luna reste disponible mais
inactif, sans bascule automatique.

### Contrôles de ce dernier ajustement

Même base locale `9fe331ee54fe920baf23cc0c472b38fc88374d03`, même branche et index
vide. Trois fichiers touchés dans cette étape : le prompt de variantes, son test
dans `tests/unit/test_ai_quality_regressions.py`, et ce rapport. Les changements
non committés des étapes précédentes sont conservés.

| Commande depuis le worktree | Résultat |
| --- | --- |
| `python -B scripts/verify_consolidation.py unit --target tests/unit/test_ai_quality_regressions.py::test_variant_prompt_example_matches_permission_to_propose_no_variants` avant correction | 1 échec attendu en 0,15 s sur l'exemple non vide |
| `python -B scripts/verify_consolidation.py unit --target tests/unit/test_ai_quality_regressions.py --target tests/unit/test_autotune_and_variants.py --target tests/unit/test_probe_llm_quality.py` | 92 passed en 0,70 s, sortie 0 |
| `ruff check --no-cache .` | `All checks passed!`, sortie 0 |
| `python -B scripts/verify_consolidation.py typecheck` | 172 fichiers et 8 scripts sans erreur, sortie 0 ; `allowed=0` |
| `make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python` | 3 463 passed, 124 skipped, 4 warnings en 99,38 s, sortie 0 |
| `git diff --check`, index et auto-revue | Vérifiés ; aucun fichier indexé |

`python` et `ruff` désignent ici les exécutables du venv existant
`/Users/aminezegmou/Projets/bot-radar/.venv/bin/`. Un premier essai ciblé avec `-k`
a été refusé par le lanceur ; le nodeid explicite ci-dessus a ensuite été utilisé.
Ruff avec son cache et deux premiers essais de typage ont échoué sur les droits
d'écriture du cache, avant analyse. Ruff a réussi sans cache, le typage avec accès
au cache du worktree, sans assouplir les gardes de configuration et de transports
du lanceur. Ces essais infructueux ne sont pas comptés comme contrôles réussis.

Les quatre avertissements sont les dépréciations préexistantes Starlette/HTTPX,
FastAPI 422 et Alembic. Les 124 tests ignorés n'attestent pas PostgreSQL réel.
Aucun réseau, secret lu, `.env` réel consulté ou modifié, commit, push ou déploiement
pendant cet ajustement. **Statut : terminé localement ; bascule non exécutée.**
