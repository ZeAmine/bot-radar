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
qualifié TTC ; « Écart estimé » affiche uniquement un montant signé : négatif sous
la référence, positif au-dessus, zéro sans signe. Aucun suffixe ni note de frais
ne suit cette ligne.
Le récap de création et sa confirmation utilisent la même carte ; alternatives
d'édition proposées uniquement à partir de deux éditions exploitables.

La création `/add` avec une demande exploitable mène directement au récapitulatif.
Sans résultat IA utilisable, le catalogue bibliographique partagé est essayé avant
la sélection manuelle de catégorie. Un ouvrage identifié, ou des candidats nommés,
ouvrent la carte Livres ; sans candidat exploitable, la catégorie reste à choisir.
Les homonymes restent distincts et les notices de repli gardent leur statut non confirmé.
Le prix maximum absent reste « Non renseigné », modifiable depuis le récapitulatif,
comme la catégorie. Un prix explicitement saisi est conservé. `/add` sans argument
conserve le choix de catégorie. Ce parcours de création ne change pas l'ingestion.

Chaque parcours de création affiche le récapitulatif avant l'analyse des variantes
et exclusions personnalisées. L'analyse automatique part une fois l'article identifié
et les homonymes départagés ; elle utilise le contexte confirmé, langue et tome compris.
Pendant l'analyse, la modification et l'annulation restent accessibles ; la création
est masquée et refusée côté serveur. Elle redevient disponible après réussite, résultat
sans apport, échec ou indisponibilité, sous réserve des autres contrôles d'identité.
La relance IA n'est proposée qu'après un échec et si le module est disponible.

Le bilan décrit les propositions supplémentaires effectivement retenues après filtrage,
sans attribuer à l'IA une décision des filtres. Il distingue un résultat sans apport
d'une absence de réponse. Les inclusions restent inchangées. Les variantes et exclusions
éditées manuellement sont protégées séparément ; les filtres de sauvegarde sont
appliqués dès leur récapitulatif pour afficher les valeurs réellement enregistrables.
Modifier le prix ou les conditions
ne relance pas l'analyse. Un changement d'identité invalide l'ancienne génération.
Une réponse tardive ne rétablit pas un brouillon annulé ou remplacé et ne modifie jamais
une alerte enregistrée. Si un écran de saisie ou un menu est ouvert, le résultat rejoint
le brouillon sans remplacer cet écran ; le prochain récapitulatif lit les valeurs courantes.
Ces tâches de création restent en mémoire, sans reprise durable après un arrêt du processus.

Le titre du panneau « Prix du marché » renvoie à une URL CAL déjà enregistrée
pour l'ISBN affiché, si elle est sûre et compatible ; sinon il reste sans lien.
L'ISBN est copiable en `<code>`. L'action d'ouverture du panneau garde son callback.
Les tomes sont présentés une fois par numéro : boutons `T1`, `T2`, etc., lignes
`Tome X : XX,XX €` ou `Tome X : Non renseigné`, sans métadonnées supplémentaires.
Une sélection ambiguë entre éditions ne reçoit ni ISBN ni prix arbitraire.

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
HTTP partagé, repli `None`, erreurs et santé. River utilise un état passif ; les
autres adaptateurs gardent leur sonde réseau en cache 30 secondes. Les
adaptateurs traduisent le dialecte ; le registre refuse un fournisseur inconnu au
démarrage. Variables : `AI_PROVIDER`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`,
`AI_TIMEOUT_SECONDS`, `AI_MAX_TOKENS`, `AI_REASONING_EFFORT`. Aucun shim historique
`DEEPSEEK_*`. Le défaut est River avec `deepseek-ai/DeepSeek-V4.1-Flash-1017`,
12 secondes par phase HTTP, 2 048 tokens de sortie et raisonnement `none`.
OpenAI/Luna est conservé mais inactif. Le plafond est un réglage initial,
pas une preuve de qualité ni de consommation optimale.

Le chemin vivant est `AIPostAlertPipeline.enrich_alert` dans `app/services/ai/`.
Avant appel, conserver `data = None` si le client n'est pas configuré.
La construction du prompt post-alerte est partagée avec la sonde de qualité ;
chaque fragment reste associé à son module.

Injecter le client via `BaseAIModule(client=...)`, pas le singleton dans un handler.
Un flag `AI_FEATURE_*` correspond à un module. Chaque appel métier transmet son
identifiant et son payload Pydantic ; les types sont stricts, les champs absents
autorisés prennent leur défaut, les champs supplémentaires sont ignorés localement.
Les listes autorisent `null`, mais leurs éléments doivent être des chaînes : un
objet ou nombre ne devient pas un mot-clé par conversion implicite.
Aucun HTTP direct dans les modules. La sonde HTTP n'atteste pas une génération.

Une complétion émet un seul POST, sans relance automatique, y compris sur 429.
Les redirections du POST sont refusées, même si le client partagé les autorise.
Le timeout reste celui du client HTTP, avec une éventuelle surcharge par appel,
sans échéance globale ajoutée. Une annulation asyncio reste une annulation.

River emploie Chat Completions avec `max_tokens`, `reasoning_effort` et les
schémas stricts documentés par son API. Le protocole est compatible OpenAI ;
le fournisseur d'inférence et de facturation journalisé est `river`, distinct du
modèle DeepSeek exécuté. Les compteurs cachés et de raisonnement sont des
sous-ensembles des totaux, jamais additionnés une seconde fois. Aucun coût calculé.

L'adaptateur OpenAI emploie Chat Completions avec `max_completion_tokens`,
`reasoning_effort` et, pour les cinq usages métier, `response_format=json_schema`
en mode strict. Il génère le schéma depuis Pydantic, rend les propriétés requises,
conserve leurs unions avec `null`, ferme les objets et retire les valeurs par défaut
filaires. La validation locale garde les réponses partielles autorisées pour les
adaptateurs et doubles existants. Une réponse structurée n'est pas une autorité métier.
Les prompts, filtres et replis restent communs aux fournisseurs.

Le dialecte `thinking` appartient uniquement à l'adaptateur DeepSeek. L'adaptateur
compatible générique utilise le mode objet JSON et le schéma dans les instructions ;
Anthropic garde son API Messages et reçoit aussi ces instructions. Tous passent
la même validation locale. Les tokens de sortie sont plafonnés par configuration.

Contrat River consulté le 24 octobre 2026 :
[endpoint et authentification](https://docs.river.ai/guides/inference/),
[modèle et compteurs](https://docs.river.ai/guides/inference-models/),
[raisonnement et sorties structurées](https://docs.river.ai/guides/inference-controls/),
[erreurs HTTP](https://docs.river.ai/guides/inference-limits/).
Le mode strict est fixé avant le POST, sans repli réseau vers `json_object` ni
OpenAI. River n'utilise plus `GET /v1/models` pour déduire la santé : cet endpoint
a été refusé alors qu'un POST réel avec la clé courante a réussi. `check_health`,
y compris avec `force=True`, ne contacte aucun fournisseur. Les états passifs
sont `unverified`, `last_call_ok` et `last_call_error`, en plus des états désactivé
et non configuré. `configured` indique la présence d'une clé, jamais un accès
confirmé. `last_call` contient l'horodatage UTC, la catégorie, le statut HTTP exact,
le code/type fournisseur expurgés et le `x-request-id` disponibles. Les messages
fournisseur libres et corps bruts sont exclus. Ces observations restent locales
au client dans le processus ; un redémarrage revient à `unverified`. Une réponse
refusée par la validation n'est pas un succès. Aucun POST automatique de santé.

`/health` conserve sa portée de vivacité HTTP : le statut applicatif reste distinct
de l'état IA, et la base, Redis, le scheduler et le poller restent explicitement
non vérifiés. `/status` distingue les observations passives d'une sonde accessible.
Les sondes et le cache des autres fournisseurs sont inchangés.

La table de raisonnement River nomme la famille Flash
sans détailler la révision `1017` ; `none` est retenu selon ce contrat de famille,
à confirmer lors de la vérification manuelle autorisée. Les pages documentent
les statuts d'erreur, pas un schéma exhaustif de leurs corps : le traitement
s'appuie sur HTTP. Seuls les identifiants d'erreur structurés autorisés sont
extraits ; aucun corps d'erreur n'est journalisé.

Références OpenAI consultées le 24 octobre 2026 :
[modèle Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Les interruptions explicites `length`, `content_filter`, `insufficient_system_resource`
et `aborted`, ainsi que les refus explicites, sont rejetés même avec un JSON valide.
L'absence historique de `finish_reason` reste compatible. Les constantes `NaN` et
`Infinity` ne sont pas acceptées comme JSON.

Chaque requête terminée émet `llm_request` avec `request_id`, `ai_module`, `provider`,
`model`, `attempt=1`, `duration_ms`, timeout effectif, code HTTP éventuel et `status`.
Le statut distingue `ok`, `timeout`, `http_error`, `invalid_json`, `empty_content`,
`validation_error`, `invalid_envelope`, `transport_error`, `refused`, `interrupted`,
`unexpected_error` et `cancelled`. `outcome` conserve le diagnostic de transport.
Pour `generate_json`, `ok` inclut parsing et validation du payload transmis ; pour
`complete` sans payload, il atteste seulement un texte reçu.

`usage.input`, `usage.output`, `usage.cached`, `usage.reasoning` relèvent les compteurs
fournis par l'API ; une absence reste `null`. Les compteurs suivent la sémantique du
fournisseur, sans estimation de coût. Ces clés préservent le masquage des noms sensibles
par `JSONFormatter`. Les logs de rejet (`llm_response`, `llm_json`, `ai_validation`)
ne contiennent ni réponse brute ni valeurs Pydantic. Le rationale des variantes
n'est pas journalisé. Les fournisseurs et modèles sont journalisés dans la frontière
LLM seulement. Une réception `text_received` n'atteste pas la validité métier.

La [procédure de migration](../operations/llm-migration.md) distingue validation locale,
qualité réelle hors production et bascule opérationnelle.

`is_series` désigne le périmètre de surveillance, pas une preuve d'identité.
L'absence de numéro ne suffit pas à prouver une série. Un tome explicitement demandé
reste un tome ; une résolution de notices numérotées peut établir une série.
Sans preuve bibliographique, `book_unidentified` conserve l'incertitude ; des
`work_choices` conservent une ambiguïté entre œuvres sans ISBN arbitraire. Un `false`
isolé ne permet donc pas d'affirmer « one-shot confirmé ». La résolution ne garantit
pas un rattrapage de toute erreur du moteur.

Les offres IA doivent avoir un montant positif et un message non vide après
nettoyage ; sinon le repli déterministe s'applique. Les scores doivent être finis,
compris entre 1 et 10, avec une revente non négative ; une charge refusée ne produit
aucune note. Les champs optionnels et les marges négatives restent acceptés.

Une estimation insuffisamment étayée s'abstient avec les trois champs numériques
à `null` (`estimated_resale_cents`, `net_profit_cents`, `pepite_score`) et une raison
non vide dans `verdict`. Ce résultat valide est distinct d'une panne et d'une sortie
vide ; il produit en arrière-plan une explication non chiffrée, sans indice pépite.
Le diagnostic `ai_scoring` distingue `abstained`, `incomplete` et `estimated`, sans
journaliser le verdict. Une réponse partielle ne produit aucune opportunité chiffrée.

Le nom filaire historique `net_profit_cents` est conservé pour compatibilité, mais
sa valeur et son affichage représentent une **marge brute estimée avant frais de
revente**, pas un bénéfice net. La marge est calculée localement comme revente
moins coût d'achat connu ; un montant fourni incohérent est refusé. Un coût inconnu
reste inconnu. Sur une source sans frais acheteur, le prix article connu sert de
coût quand le total estimé manque ; aucune formule de frais n'est modifiée.

Pour les variantes/exclusions, un chargement précède l'attente ; verrou anti-double
clic. `answered` distingue un avis vide d'une panne. Une réponse ne produisant aucun
terme utilisable reste une réponse, filtrée avec ses raisons. Les variantes peuvent être absentes sans
remplissage artificiel. Les propositions initiales et le résultat motivé du filtre
restent disponibles dans le résultat de module pour la sonde ; les filtres du domaine
et la normalisation de `app/utils/text.py` gardent la décision finale. La sonde distingue
absence de proposition, liste entièrement ou partiellement filtrée, répétitions
normalisées et variantes conservées. Une liste vide ne prouve pas à elle seule sa
pertinence ; une exclusion générique s'évalue dans son contexte, sans interdiction
lexicale générale. Le récap dit le verdict
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
