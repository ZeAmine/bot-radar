# Retrait d'ISBN et résultats différés (F06)

Statut : contrat actuel. Périmètre : retrait ISBN, résultats tardifs et limites de la garde de version ; aucun changement de matching.

Un changement réel d'ISBN détache uniquement les AlertBook de l'alerte et remet
son total de série et son verdict de catalogue à NULL, dans la transaction
d'édition sous verrou. Les Book partagés, leurs cotes/historiques et les autres
liaisons restent intacts. Le récapitulatif relit les relations après commit.
Une répétition du retrait sur un ISBN déjà NULL n'efface pas une découverte
par titre arrivée entre-temps ; aucune réparation historique automatique.

Au retrait, titre et critères manuels sont conservés ; seuls les termes exactement
égaux à l'ancien ISBN sont retirés des inclusions, variantes et exclusions.
Les propositions non appliquées expirent après commit, pas après un commit échoué.
Les résultats IA ajoutent leurs nouveaux termes sans vider les critères appliqués.
Le remplacement confirmé conserve son réalignement de titre/inclusions publié,
mais ne vide plus globalement les variantes/exclusions appliquées. L'exception
de saisie volontaire pendant une panne (ADR-038) reste inchangée.

Une préparation existante reçoit un nouveau jeton ; au retrait, son auteur
explicitement demandé et l'intention d'activation sont conservés. Une préparation
NULL reste NULL. Le worker F04 ne peut écrire/activer que sa version pending
toujours demandée ; les suggestions explicites restent possibles en pause/blocage.

Les consultations restent hors verrou SQL. Avant liaison, verdict ou écriture
de suggestions, le résultat est confronté à l'alerte relue sous le même verrou
de ligne que l'édition. La garde conserve nom, ISBN, statut, jeton de préparation
et updated_at ; elle relit aussi les listes utilisées par les fusions, sans
expirer les relations User chargées pour l'envoi. Elle couvre le chemin historique,
le helper vendeur F07, les liaisons locales de l'ingestion et du panneau marché,
la consultation/confirmation d'édition et les propositions en mémoire.

La seule égalité finale de l'ISBN après A → retrait → A n'autorise pas l'écriture :
le jeton F04 doit correspondre, ou, pour une ligne NULL, updated_at doit aussi
correspondre à la révision capturée avant consultation ou mise en file locale.
Les consultations et les écritures sont idempotentes sur la version applicable.
Une nouvelle découverte par titre peut légitimement retrouver le même Book.
Une cotation engagée peut finir sur ce Book partagé sans recréer une liaison.

## Limites du contrôle sans migration

updated_at est un horodatage existant, pas une génération monotone dédiée.
La garde est conservatrice : une autre écriture sur l'alerte peut faire abandonner
un résultat pourtant bibliographiquement applicable. Elle protège le scénario ABA
dans les parcours testés où les écritures font évoluer updated_at ; elle ne prouve
pas universellement l'appartenance à une demande en cas de collision d'horodatage,
recul d'horloge ou écriture hors contrat rétablissant cette valeur.
Une garantie durable indépendante de l'horloge demanderait un arbitrage sur une
révision dédiée ; aucune migration ni infrastructure ajoutée dans F06.
Les anciens boutons de confirmation ISBN ne portant pas le contexte de révision
expirent plutôt que d'être ratifiés sans preuve suffisante.

La revue et les tests de concurrence PostgreSQL restent nécessaires ; SQLite ne
valide pas le verrou de PostgreSQL. Aucun changement de matching, seuil ou
configuration, aucun appel réseau avant Telegram et aucune durabilité après crash
n'est ajouté par ce lot.
