# Contrat Telegram et IA

Statut : contrat actuel. Périmètre : présentation, propositions, variantes et frontière IA.
Le flux d'envoi est défini dans [le contrat d'alerte](alert-pipeline.md).

## Présentation et gestes explicites

Clavier d'annonce : « Voir l'annonce » ; ligne BD « Prix du marché » ; dernière
ligne « Faux positif » et « Négocier ».
Ces libellés restent sans émoji, ainsi que « Signalé », y compris après annulation ou
confirmation du faux positif. Les autres émojis restent. Aucun bouton factice « Payer »
ou « Dressing / Lot ». Le bot ne paie jamais.

La charte canonique est [labels](../../app/bot/labels.py), dont la docstring est
consommée par [test_charte_copy](../../tests/unit/test_charte_copy.py) : libellés de
champs gras, valeurs romaines, consignes en italique, avertissements isolés en italique.
La série est une indication hors de la liste d'informations. Le prix livré est
qualifié TTC ; le marqueur d'écart ferme la ligne, sans approbation verte à écart nul.
Le récap de création et sa confirmation utilisent la même carte ; alternatives
d'édition proposées uniquement à partir de deux éditions exploitables.

Le signalement faux positif est confirmé/annulable. Le scan périodique et le
déclenchement après signalement attendent le **troisième** faux positif d'une même
alerte ; une analyse demandée explicitement par `/autotune` reste possible avant.
L'auto-tuning produit une **proposition** : application telle quelle ou édition
manuelle, jamais modification silencieuse des critères. La liste éditée remplace la
suggestion ; à l'application, elle rejoint les exclusions déjà enregistrées et le
plafond des propositions ne peut pas en tronquer la fin. La confirmation affiche la
liste réellement enregistrée. Les dislikes sont purgés atomiquement à l'application,
conservés à l'annulation. Une analyse échouée, vide ou ne proposant aucune
exclusion nouvelle conserve aussi les signalements. Exclusions copiables en `<code>`.
Scan et clic partagent la construction de proposition ; un créneau Redis atomique est acquis avant l'IA,
refusé si Redis manque, relâché si rien n'est émis. Les variantes proposées restent
jetables en mémoire ; les exclusions ont leur store Redis.

## Hygiène et identité des mots-clés

[keyword_hygiene](../../app/domain/rules/keyword_hygiene.py) nettoie tous les chemins
d'écriture, manuels ou IA : déduplication normalisée, plafonds, ISBN étrangers,
énumérations dégénérées, exclusions numériques ou d'une lettre. Les raisons sont
produites par la même passe que la décision. Les inclusions restent des critères,
les variantes des écritures alternatives, les exclusions des rejets.
Une exclusion ne peut avaler le produit ou le titre suivi d'un numéro ; la suppression
d'une variante répétant le titre n'a pas la même sémantique qu'une exclusion.

La garde d'identité des variantes prend le titre et les inclusions comme références :
nombres compatibles à l'écriture **et** en lecture ; au moins un mot reconnaissable
à l'écriture seulement. Préserver cette asymétrie. La qualification par numéro doit
conserver l'orthographe originale, pas les positions d'une forme normalisée de
longueur différente. Les nettoyeurs ne doivent pas devenir des migrations implicites.

La saisie manuelle des variantes dans l’édition existante ne donne pas accès à une
nouvelle suggestion IA : la branche `ai_variants` subsiste mais aucun clavier ne
l’émet. Le menu sans action IA est protégé par `test_telegram_regressions.py` ; rendre
cette branche accessible demande un arbitrage distinct, pas un simple nettoyage.

## Frontière IA et absence de réponse

[LLMClient / BaseLLMClient](../../app/providers/llm/base.py) possèdent activation,
HTTP partagé, repli `None`, erreurs et sonde de santé en cache 30 secondes. Les
adaptateurs traduisent le dialecte ; le registre refuse un fournisseur inconnu au
démarrage. Variables : `AI_PROVIDER`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`,
`AI_TIMEOUT_SECONDS`. Aucun shim historique `DEEPSEEK_*`.

Le chemin vivant est `AIPostAlertPipeline.enrich_alert` dans `app/services/ai/`.
Avant appel, conserver `data = None` si le client n’est pas configuré.

Injecter le client via `BaseAIModule(client=...)`, pas le singleton dans un handler.
Un flag `AI_FEATURE_*` correspond à un module. Chaque JSON est validé avec champs
optionnels et défauts ; pas d'HTTP direct dans les modules.
La sonde HTTP n'atteste pas une génération.

Une complétion émet un seul POST, sans relance automatique ; les variantes suivent
la même limite et conservent leur repli sans réponse. Le timeout reste celui du
client HTTP, avec une éventuelle surcharge par appel, sans échéance globale ajoutée.
L'adaptateur compatible conserve le plafond de sortie implicite du fournisseur ;
aucune optimisation de consommation n'est attestée. Les interruptions explicites
`length`, `content_filter`, `insufficient_system_resource` et `aborted` sont refusées,
même si leur contenu forme un JSON valide. L'absence de ce champ reste compatible.
Référence du dialecte : [API DeepSeek](https://api-docs.deepseek.com/api/create-chat-completion/),
consultée le 18 octobre 2026.

Les logs distinguent la fin de requête (`llm_request`, durée, timeout effectif,
statut HTTP et classe d'erreur), le rejet d'enveloppe ou de contenu (`llm_response`),
le JSON refusé (`llm_json`) et la validation métier refusée (`ai_validation`).
`text_received` atteste seulement la réception d'un texte, pas sa validité métier.
Les diagnostics de rejet ne contiennent ni réponse brute ni valeurs Pydantic.
Les tokens consommés ne sont pas relevés dans ce lot.

Les offres IA doivent avoir un montant positif et un message non vide après
nettoyage ; sinon le repli déterministe s'applique. Les scores doivent être finis,
compris entre 1 et 10, avec une revente non négative ; une charge refusée ne produit
aucune note. Les champs optionnels et les marges négatives restent acceptés.

Pour les variantes/exclusions, un chargement précède l'attente ; verrou anti-double
clic. `answered` distingue un avis vide d'une panne. Une réponse ne produisant aucun
terme utilisable reste une réponse, filtrée avec ses raisons. Le récap dit le verdict
et n'annonce un bouton de relance que si le clavier l'offre. L'IA n'est jamais une
autorité ISBN ; le contexte confirmé ancre ses propositions. Les suggestions tardives
restent soumises aux gardes de saisie et d'identité avant écriture.

Preuves : [contrat de chaque adaptateur](../../tests/unit/test_llm_contract.py),
[registre](../../tests/unit/test_llm_registry.py),
[charte](../../tests/unit/test_charte_copy.py),
[matching](../../tests/unit/test_matching_rules.py).
Ces tests synthétiques ne vérifient ni disponibilité réelle du moteur ni écran Telegram.

## Création et remplacement d'identité

La saisie originale et les requêtes de recherche restent distinctes du titre
canonique confirmé. Un ISBN explicitement saisi se résout directement. Un titre
artificiel ou un brouillon non identifié ne peut pas opposer un conflit à la notice.
L'ISBN proposé par l'IA est ignoré ; seule une autorité confirme l'identité.

Un conflit avec une identité établie exige un choix explicite, lié à la proposition
et revalidé avant remplacement. Les critères dérivés suivent le titre canonique.
Les résultats asynchrones vérifient le jeton et la révision avant application.
Le récap reprend les critères courants, y compris les conditions modifiées
pendant une recherche de variantes.
Un choix de catégorie conserve l'identité reçue avec le callback avant son
acquittement. Cette identité est revalidée sous `draft_guard` avant mutation ; les
recherches déclenchées gardent leur jeton jusqu'à l'application. Leurs appels réseau
restent hors de la garde. Une ancienne interaction ne reprend pas l'identité d'une
nouvelle commande `/add`.
Au sein d'un même brouillon, la phase de choix de catégorie et la génération de
recherche sont relevées avant l'acquittement puis vérifiées sous la garde avant
application. Un clic déjà en attente ne peut reprendre la main après un choix de
catégorie plus récent, même si le jeton de création n'a pas changé.
`/skip` dans le champ ISBN retire ce code et le conflit en attente, puis affiche
le récap actualisé ; ce geste ne transforme pas un one-shot en série.

L'hygiène des exclusions reçoit aussi l'autrice et l'éditeur confirmés : le contrôle
porte sur la fusion finale des termes, avec la même correspondance par mots entiers
que le matching. Une exclusion qui rejetterait ces mentions correctes est retirée.
Il ne s'agit pas d'un nettoyage massif des alertes existantes.
