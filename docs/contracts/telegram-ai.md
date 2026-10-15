# Contrat Telegram et IA

Statut : contrat actuel. Périmètre : présentation, propositions, variantes et frontière IA.
Le flux d'envoi est défini dans [le contrat d'alerte](alert-pipeline.md).

## Présentation et gestes explicites

Clavier d'annonce : « Acheter » (callback uniquement si configuré) et « Voir l'annonce » ;
ligne BD « Prix du marché » ; dernière ligne « Faux positif » et « Négocier ».
Ces libellés restent sans émoji, ainsi que « Signalé », y compris après annulation ou
confirmation du faux positif. Les autres émojis restent. Aucun bouton factice « Payer »
ou « Dressing / Lot ». Le [checkout](../vinted-checkout.md) ne paie jamais.

La charte canonique est [labels](../../app/bot/labels.py), dont la docstring est
consommée par [test_charte_copy](../../tests/unit/test_charte_copy.py) : libellés de
champs gras, valeurs romaines, consignes en italique, avertissements isolés en italique.
La série est une indication hors de la liste d'informations. Le prix livré est
qualifié TTC ; le marqueur d'écart ferme la ligne, sans approbation verte à écart nul.
Le récap de création et sa confirmation utilisent la même carte ; alternatives
d'édition proposées uniquement à partir de deux éditions exploitables.

Le signalement faux positif est confirmé/annulable. L'auto-tuning produit une
**proposition** : application telle quelle ou édition manuelle, jamais modification
silencieuse des critères. Les dislikes sont purgés atomiquement à l'application,
conservés à l'annulation. Exclusions copiables en `<code>`. Scan et clic partagent
la construction de proposition ; un créneau Redis atomique est acquis avant l'IA,
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
Le chemin `ListingAnalysisProvider` + `AsyncEnrichListingUseCase.execute` sert aux
tests/provider structuré avec cache ; ne pas le recâbler sur l’ingestion.
Avant appel, conserver `data = None` si le client n’est pas configuré.

Injecter le client via `BaseAIModule(client=...)`, pas le singleton dans un handler.
Un flag `AI_FEATURE_*` correspond à un module. Chaque JSON est validé avec champs
optionnels et défauts ; pas d'HTTP direct dans les modules. La clé d'analyse inclut
fournisseur, modèle et version de prompt. La sonde HTTP n'atteste pas une génération.

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
