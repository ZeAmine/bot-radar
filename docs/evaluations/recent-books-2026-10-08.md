# Identification des livres récents — évaluation du 8 octobre 2026

## Périmètre et méthode

Référence : `4eed61f7f124de764173050ce106ef52dbf08ace`. Branche de travail :
`fix/recent-book-identification`. Lot indépendant de F04 : aucune nouvelle source,
aucun cache applicatif, aucune route, migration ou modification du chemin d'annonce.

Les 16 couples titre/auteur et ISBN de référence ont été fixés avant les appels.
Les références documentaires et les résultats détaillés sont dans
[recent-books-2026-10-08.json](recent-books-2026-10-08.json).
La référence de *La Bête* est secondaire (Bdphile) ; celle de *Le Chat* est une
notice de bibliothèque. Leur indépendance ne suffit pas à certifier chaque détail
éditorial. Les autres références viennent d'éditeurs ou d'institutions bibliographiques.

48 requêtes publiques BNF/OpenLibrary, séquentielles, espacées d'au moins 1,05 s,
sans retry, clé ni accès production. Toutes ont répondu HTTP 200. Google Books
est désactivé ; aucun secret local n'a été chargé. Aucun appel Babelio, collecte
de critiques, notes, résumés ou profils. Les requêtes ne demandent que les champs
bibliographiques déjà utilisés par le service.

Pour chaque titre : recherche ISBN avec contrôle du titre, puis titre avec auteur.
Les réponses HTTP sont réutilisées dans le **harnais de diagnostic uniquement**
pour comparer la résolution par titre du commit de référence au code corrigé.
Il ne s'agit ni d'un cache du bot ni d'un second passage public. La baseline n'est
pas chronométrée à froid une deuxième fois : seuls les statuts sont comparés.

Paramètres reproductibles : BNF SRU `version=1.2`, `operation=searchRetrieve`,
`recordSchema=dublincore`, `maximumRecords=10`, `bib.isbn adj` ou `bib.title all`
(puis `bib.anywhere all` selon le service) ; OpenLibrary Search, `limit=10`,
`title,author_name,publisher,first_publish_year,isbn,number_of_pages_median,language`.
Les ISBN-10 des notices de référence sont convertis en ISBN-13 avec contrôle de clé.

## Résultats

| Titre / auteur | Titre avant → après | ISBN exact | Titre : secondes HTTP |
| --- | --- | --- | ---: |
| Les Enfants perdus — Neyef | ambigu → ambigu | absent | 0,269 |
| Dailycieux — Whoogy's | absent → absent | absent | 0,309 |
| L'Enfance des ruines — Pascal Rabaté | ambigu → ambigu | absent | 1,298 |
| La Langue des vipères — Juliette Brocal | trouvé → trouvé | trouvé | 0,052 |
| Hoka Hey ! — Neyef | trouvé → trouvé | trouvé | 0,559 |
| La Couleur des choses — Martin Panchaud | trouvé → trouvé | trouvé | 0,884 |
| La Saga de Grimr — Jérémie Moreau | trouvé → trouvé | trouvé | 0,439 |
| Le Monde sans fin — Jean-Marc Jancovici | trouvé → trouvé | trouvé | 0,817 |
| La Bête — Chabouté | trouvé → trouvé | trouvé | 1,043 |
| Le Chat — Philippe Geluck | trouvé → trouvé | trouvé | 0,794 |
| Le Voyage — Edmond Baudoin | **ambigu → trouvé** | trouvé | 1,184 |
| La Chute — Albert Camus | trouvé → trouvé | trouvé | 0,702 |
| La Guerre éternelle — Marvano | ambigu → ambigu | non confirmé pour le titre attendu | 1,001 |
| Dune — Frank Herbert | trouvé → trouvé | non confirmé pour le titre attendu | 0,346 |
| La Peste — Albert Camus | trouvé → trouvé | trouvé | 0,603 |
| L'Étranger — Albert Camus | trouvé → trouvé | trouvé | 0,637 |

- **12/16 œuvres retrouvées par titre après correction, contre 11/16 avant.**
  Le gain réel est *Le Voyage* d'Edmond Baudoin : le repli OpenLibrary est désormais
  interrogé malgré les homonymes BNF d'autres auteurs.
- **11/16 ISBN exacts confirmés pour le couple demandé.** Ce chiffre ne signifie
  pas que les pages, l'année ou l'éditeur de tous les replis sont prouvés au niveau
  édition : OpenLibrary fournit aussi des métadonnées agrégées par œuvre.
- **8/16 éditions de référence figurent dans les résultats par titre.** Une œuvre
  retrouvée n'est donc pas une édition retrouvée. *Le Chat*, *Dune*, *La Peste* et
  *L'Étranger* rendent d'autres ISBN lors de la recherche par titre ; ce comportement
  existait avant le lot. Le titre seul n'avait pas demandé l'ISBN de référence.
- Aucun auteur étranger à l'œuvre demandée n'a été nouvellement confirmé dans cet
  échantillon. Les 11 confirmations ISBN portent le code demandé. Cela ne prouve
  pas l'absence générale de mauvais rapprochements, ni la complétude des éditions.
- Durée HTTP cumulée médiane : **0,218 s par recherche ISBN**, **0,670 s par titre** ;
  maxima : 0,701 s et 1,298 s. Ces mesures excluent l'espacement volontaire, Telegram,
  le moteur de mots-clés et la préparation des cotes. Ce ne sont pas des mesures du
  parcours utilisateur complet. Pas de mesure de réponse en cache : il n'existe pas.

*La Langue des vipères* est parue le 15 avril 2026, et non cette semaine : elle sert
de témoin récent dans le groupe initial « nouveautés ». Les trois sorties de
septembre/octobre non identifiées restent non confirmées après le lot.

## Limites et suite motivée

Les réponses `not_found` de *La Guerre éternelle* et *Dune* portent des éléments
bruts BNF : elles ne prouvent pas une absence de catalogue. La concordance du titre
et les mentions d'édition restent à examiner séparément ; le seuil global n'a pas
été abaissé. La recherche est bornée aux 10 notices BNF rendues, pas à tout le catalogue.

La valeur d'une source complémentaire pour les nouveautés reste démontrable sur
les métadonnées documentaires, mais pas sur un adaptateur automatisé inexistant.
La [fiche Rue de Sèvres de Neyef](https://www.editions-ruedesevres.fr/Les-Enfants-Perdus)
porte l'édition standard et l'ISBN exact. Autre découverte utile :
[Dailycieux figure dans Nouveautés Éditeurs de la BNF](https://nouveautes-editeurs.bnf.fr/accueil?id_declaration=10000001392716&titre_livre=Dailycieux),
comme **déclaration éditeur**, alors qu'il reste absent des requêtes SRU utilisées.
Ce n'est pas une confirmation de dépôt légal et aucune intégration n'a été ajoutée.

Ces constats justifient un futur prototype borné de métadonnées éditeur, uniquement
après vérification de l'interface et des droits d'accès/réutilisation. Ils ne
justifient ni la promotion automatique de Babelio ni un remplacement des autorités.

## Validation du lot

Les régressions automatisées traversent le vrai service et le contrat partagé,
avec un transport HTTP simulé : homonymes avec/sans auteur explicite, repli suivant
si le premier nomme un autre auteur, sources indisponibles, BNF muette et candidat
non confirmant, contradiction d'ISBN, absence réelle, effacement de l'ancienne
identité, rendu des champs inconnus et absence de fuite d'exception dans les logs.

Le contrat `BookSearchResult` n'a pas changé : le rapport existant suffit à porter
les nouvelles tentatives. L'ISBN volontairement saisi reste conservé ; un candidat
ne remplit pas les champs confirmés. La carte propose de retirer cet ISBN pour une
surveillance textuelle. Aucun ISBN candidat n'est automatiquement ratifié.

Vérifications finales : **137 tests ciblés**, puis **1 232 tests réussis**, un test
PostgreSQL ignoré (serveur de test non fourni), 15 avertissements. `ruff check .`
et `mypy app` passent, zéro erreur de typage sur 155 fichiers. Le passage initial
sans jetons factices échouait sur l'authentification des routes ; les tests ont été
rejoués avec les quatre valeurs publiques de test de la CI et le chargement dotenv
désactivé avant tout import applicatif. Aucun accès production requis.
