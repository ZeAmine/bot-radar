# Annonces de parution « Nouveautés Éditeurs » — évaluation du 30 octobre 2026

Statut : évaluation. Périmètre : déclarations et appels publics décrits sur la
révision citée ; aucune de ces requêtes n'est rejouée par cette documentation.
Aucun code de production n'est appelé par la mesure.

## La question posée

`add` peut-il accepter un livre paru depuis quelques jours que le catalogue BNF ne
connaît pas encore, sans deviner son identité ? Et
[Nouveautés Éditeurs de la BnF](https://nouveautes-editeurs.bnf.fr/) — le service
où les éditeurs déclarent leurs parutions — porte-t-il assez d'information pour
fonder cette identité ?

L'[évaluation du 8 octobre 2026](recent-books-2026-10-08.md) avait laissé la
question ouverte sur un cas précis : *Dailycieux* figurait dans Nouveautés
Éditeurs comme déclaration d'éditeur, tout en restant absent des requêtes SRU
utilisées. Ce constat isolé ne disait ni la fréquence du phénomène, ni ce que les
déclarations contiennent réellement.

## Périmètre et méthode

Référence : `bc19539396f7170dcf72b8cafce674079a3ff326` (main au 30 octobre 2026),
avant toute modification du service. La collecte est faite hors du dépôt, par un
script ponctuel non versionné ; ce fichier et son
[JSON](nouveautes-editeurs-2026-10-30.json) portent les déclarations lues, les
verdicts du catalogue et le journal des requêtes.

Le corpus est construit **par dates de parution, pas par mots-clés**. Une recherche
par mots-clés choisirait les livres dont on sait déjà qu'ils existent et ne dirait
rien du cas qui nous occupe : un livre récent, un catalogue muet. Neuf dates ont
été retenues dans les 90 jours précédant la référence — 07/08, 21/08, 04/09,
18/09, 25/09, 02/10, 09/10, 16/10 et 23/10/2026 —, une seule page de grille lue
par date, dédoublonnage en tourniquet, puis la fiche de chaque déclaration retenue
lue jusqu'à 34 déclarations de « Livre imprimé ». Aucune pagination, aucune liste
parcourue, aucun flux RSS.

Deux sous-échantillons complètent ces dates : **8 déclarations d'éditeurs de bande
dessinée** (Misma, Cornélius, Le diplodocus), et **3 déclarations** trouvées par le
titre « Les Enfants perdus ». La BD est donc représentée, mais par une **déduction** :
le site ne déclare pas le genre — toutes les fiches lues portent le type « Livre
imprimé », aucune ne porte de champ « genre ». L'appartenance d'un éditeur à la BD
est le seul indice disponible.

Chaque déclaration est ensuite confrontée au catalogue BNF **par son ISBN**
(`bib.isbn adj`), avec `version=1.2`, `operation=searchRetrieve`,
`recordSchema=dublincore`, `maximumRecords=10`.

170 requêtes publiques au total — 88 sur `nouveautes-editeurs.bnf.fr`, 82 sur le SRU
du catalogue —, toutes séquentielles, espacées d'au moins 1,05 s, sans reprise.
Toutes ont répondu HTTP 200. Durée cumulée 32,2 s, médiane 0,134 s, maximum 3,30 s.
Aucune clé, aucun accès à la production, aucune écriture.

### Le protocole de recherche est vérifié, pas supposé

Le site tient son état de recherche **en session**. Sans amorçage, il répond sa
liste par défaut : une recherche sur un ISBN a rendu « 284 résultats » et 20
identifiants, tout en enregistrant le filtre demandé dans son état (`filtersList`
contenait bien le code). Une recherche commence donc par un `GET /` qui ouvre une
session neuve, puis un `POST /accueil` urlencodé (`refineValue`, `category=FULL_TEXT`,
`action=ADD_FILTER`, `onRebondBySearch=true`, `modeAffichage=gridMode`, `page=1`,
`minRange=90`, `maxRange=90`). Trois requêtes HTTP au plus par recherche —
amorçage, recherche, fiche —, jamais davantage, et un seul appel logique par étape.

Conséquence pour la lecture : **zéro identifiant avec un compteur à zéro est une
absence ; des dizaines d'identifiants sont un défaut de protocole, jamais une
absence.** Le compteur affiché est la seule preuve que la recherche a porté, et il
doit rester petit pour un terme précis.

Auto-contrôle rejoué le 30 octobre, sortie brute dans le JSON :

| Requête | Déclarations rendues | Compteur affiché | Filtre dans la page |
| --- | ---: | ---: | --- |
| `9782812322495` | 1 | 1 | oui |
| `Dailycieux` | 1 | 1 | oui |
| `Les Enfants perdus` | 3 | 3 | oui |
| `Neyef` | 0 | 0 | oui |
| `L'Enfance des ruines` | 0 | 0 | oui |
| *contrôle négatif* : `9782812322495` sans amorçage | 20 | 284 | oui |

Le contrôle négatif est le point sensible : **le filtre est enregistré mais pas
appliqué**. Une implémentation qui lirait le filtre dans la page conclurait à tort
que la recherche a porté. Seul le compteur le dit.

## Résultats

45 déclarations lues, dont **40 dans la fenêtre de 90 jours**. 38 portent un ISBN-13
valide au sens de la clé de contrôle ; 2 n'en portent aucun.

**5 des 38 déclarations de la fenêtre sont au catalogue par leur ISBN (13 %), 33 n'y
sont pas (87 %).** C'est la mesure qui justifie le besoin : pour la très grande
majorité des parutions récentes, le catalogue ne rend rien.

Le dépôt légal ne se confond pas avec le catalogage : sur les 6 déclarations
« Reçu par la BnF », 4 sont au catalogue par ISBN et 2 n'y sont pas ; réciproquement,
1 des 5 déclarations présentes au catalogue n'est qu'« Annoncé éditeur ». L'état de
dépôt est donc une information de provenance, jamais une preuve de notice.

La bande dessinée suit la même pente : 5 déclarations d'éditeurs de BD dans la
fenêtre, **1 seule au catalogue par ISBN**, 4 absentes.

Les valeurs de ce paragraphe sont mesurées, sauf deux qui sont déduites et signalées
comme telles : l'appartenance à la BD (déduction par l'éditeur), et le corpus lui-même
(une page de grille par date, donc un échantillon des parutions du jour, pas leur total).

### Déclarations présentes au catalogue par ISBN (5)

| Parution | Éditeur | Titre — auteur | ISBN-13 | Dépôt légal | Catalogue BNF par ISBN |
| --- | --- | --- | --- | --- | --- |
| 21/08/2026 | les Éditions des Éléphants | Le Problème avec les girafes — Mantchev Lisa, Yoo Taeeun | 9782372731881 | Reçu par la BnF | **présent** (1 notice) |
| 21/08/2026 | les Éditions des Éléphants | Zip Zap Flip & Flap — Diffee Matthew | 9782372731867 | Reçu par la BnF | **présent** (1 notice) |
| 21/08/2026 | Le diplodocus | À qui est cette langue ? — Sabbagh Clémence, Schnellb | 9791094908600 | Reçu par la BnF | **présent** (2 notices) |
| 28/09/2026 | Voir de Près | Abi et les enfants perdus — Feraga Alexandre | 9782378289089 | Reçu par la BnF | **présent** (1 notice) |
| 02/10/2026 | Le diplodocus | Rodéo limace — Galoo Axelle | 9791094908617 | Annoncé éditeur | **présent** (1 notice) |

### Déclarations absentes du catalogue par ISBN (33)

| Parution | Éditeur | Titre — auteur | ISBN-13 | Dépôt légal | Catalogue BNF par ISBN |
| --- | --- | --- | --- | --- | --- |
| 07/08/2026 | Leng XIONG, Éditions du Bambou … | Comment survivre à une rupture amoureuse sans… — XIONG Leng | 9791098700958 | Annoncé éditeur | absent (0 notice) |
| 07/08/2026 | Librinova | La Toute Première Enquête du Lieutenant Stagi… — Hue Hervé | 9791043910227 | Reçu par la BnF | absent (0 notice) |
| 07/08/2026 | Librinova | OVNI : Divulgation — Valiela Sébastien | 9791043913136 | Annoncé éditeur | absent (0 notice) |
| 07/08/2026 | Librinova | Seule à Ibestad : Île de Rolla - Norvège — Morskaïa Sil'viya, Gollet  | 9791043910890 | Annoncé éditeur | absent (0 notice) |
| 07/08/2026 | Books on Demand | Une Amitié Japonaise Particulière : 50 ans d'… — Brest de Kergreac'h Anne,  | 9782322843459 | Annoncé éditeur | absent (0 notice) |
| 21/08/2026 | Norma Éditions | Fadia Ahmad. Échoes-Echoes — Le Thorel Pascale | 9782376661207 | Annoncé éditeur | absent (0 notice) |
| 21/08/2026 | Éditions Cornélius | Perle de Pierre — CHALUMEAU Louis | 9782360812479 | Annoncé éditeur | absent (0 notice) |
| 21/08/2026 | Fage éditions | Nicolas Rubinstein — Rubinstein Nicolas | 9782849758465 | Annoncé éditeur | absent (0 notice) |
| 21/08/2026 | Misma | Le sel des jours — Martin Sandrine | 9782494740259 | Annoncé éditeur | absent (0 notice) |
| 04/09/2026 | le Lys bleu éditions | Couple, communication consciente et bienveill… — Mouton-Akotiai Genevieve | 9791044044815 | Annoncé éditeur | absent (0 notice) |
| 04/09/2026 | le Lys bleu éditions | Le déséquilibre vertueux : Pour ceux qui font… — Métayer Antoine | 9791043755767 | Annoncé éditeur | absent (0 notice) |
| 04/09/2026 | Fage éditions | Aimer hier : Notes pour une histoire du senti… — Anders Günther, Kalinowski | 9782849758410 | Annoncé éditeur | absent (0 notice) |
| 04/09/2026 | Editions Chandeigne & Lima | Dracaena Draco et autres histoires africaines… — Fortes Erickson, Barros Gi | 9782367323190 | Annoncé éditeur | absent (0 notice) |
| 04/09/2026 | Organisme Professionnel de Prév… | DALLAGISTE : Rapport d'observation selon la m… — — | 9782735405923 | Reçu par la BnF | absent (0 notice) |
| 11/09/2026 | Misma | HOTPOT — Cong Yan, Cong Yan, Aloisi | 9782494740273 | Annoncé éditeur | absent (0 notice) |
| 18/09/2026 | Books on Demand | La Cuisine de Famille et des Gourmets : Se Ré… — Monni Raphaël | 9782322856893 | Annoncé éditeur | absent (0 notice) |
| 18/09/2026 | Beta Publisher | Ana is lost — Feitelson Samantha | 9782383921332 | Annoncé éditeur | absent (0 notice) |
| 18/09/2026 | Fage éditions | Paul Cezanne dépeint par ses contemporains — Cezanne Philippe | 9782849758427 | Annoncé éditeur | absent (0 notice) |
| 25/09/2026 | Citadelles & Mazenod | Cezanne et les peintres : Une histoire d'infl… — Semmer Laure-Caroline | 9782386111044 | Annoncé éditeur | absent (0 notice) |
| 25/09/2026 | Books on Demand | Les Frappés : 10 questions à 100 (+1) femmes … — Blanchard Loïc | 9782322612819 | Annoncé éditeur | absent (0 notice) |
| 25/09/2026 | Aelia Vela | Lexique des réalités invisibles : Un témoignage — — | 9791098671937 | Annoncé éditeur | absent (0 notice) |
| 02/10/2026 | Seuil / Olivier / Points / Nouv… | Le Sac d'Ashley : Un souvenir de famille du t… — Miles Tiya | 9782021569629 | Annoncé éditeur | absent (0 notice) |
| 02/10/2026 | Éditions du Palémon | Chapeaux pointus et coups tordus — Lehoulier Pierre, Naudinat | 9782371333796 | Annoncé éditeur | absent (0 notice) |
| 02/10/2026 | Norma Éditions | André Lhote : L'art de la controverse — Garcia Françoise | 9782376661214 | Annoncé éditeur | absent (0 notice) |
| 02/10/2026 | Books on Demand | Espoir d'elle : Recueil — Darmon Jean Paul | 9782322613205 | Annoncé éditeur | absent (0 notice) |
| 09/10/2026 | Association loi 1901 Francis Dé… | Addictions, l'addition ! — DÉCAMPS Francis | 9791098860607 | Annoncé éditeur | absent (0 notice) |
| 16/10/2026 | Chloé Bellefontaine | Le silence des blessures : Quand les blessure… — — | 9791043914263 | Annoncé éditeur | absent (0 notice) |
| 16/10/2026 | JP Besnardi | Les Ombres de Nerion — , Klobcar Peter | 9791097997106 | Annoncé éditeur | absent (0 notice) |
| 16/10/2026 | Norma Éditions | Tisser les modernités/Weaving modernities — Fontès Nathalie, Chich Eli | 9782376661252 | Annoncé éditeur | absent (0 notice) |
| 16/10/2026 | GWEN DO | En cherchant la rosée... — — | 9782959396915 | Annoncé éditeur | absent (0 notice) |
| 16/10/2026 | Misma | Les choses de l'amour (édition 2026) — de Monfreid Dorothée, De M | 9782494740310 | Annoncé éditeur | absent (0 notice) |
| 16/10/2026 | Misma | FABLES SANS ANIMAUX — De Monfreid Dorothée, De M | 9782494740280 | Annoncé éditeur | absent (0 notice) |
| 16/10/2026 | Éditions Cornélius | L'Insupportable Grabote : Volume 1 - 1973-1976 — CLAVELOUX Nicole | 9782360812486 | Annoncé éditeur | absent (0 notice) |

### Déclarations sans ISBN exploitable (2)

| Parution | Éditeur | Titre — auteur | ISBN-13 | Dépôt légal | Catalogue BNF par ISBN |
| --- | --- | --- | --- | --- | --- |
| 18/09/2026 | Laure SALLAHI | Manifester l'or : l'oeuvre de manifester — SALLAHI Laure | **aucun** | Annoncé éditeur | non interrogé |
| 23/10/2026 | AF FLUVIAL CONSEIL - AURELIE FERAY | Cap sur la Seine en Bateau — Feray Aurélie | **aucun** | Annoncé éditeur | non interrogé |

### Témoins hors fenêtre de 90 jours (5)

| Parution | Éditeur | Titre — auteur | ISBN-13 | Dépôt légal | Catalogue BNF par ISBN |
| --- | --- | --- | --- | --- | --- |
| 13/05/2026 | Le diplodocus | L'envol — Casteldaccia Sophie, Belle | 9791094908594 | Annoncé éditeur | **présent** (1 notice) |
| 17/07/2026 | Dashbook | Raíz & fe en tierra ajena : Fe, maternidad y … — Chus Chadia | 9782385896829 | Reçu par la BnF | **présent** (1 notice) |
| 24/07/2026 | Noir'édition | Les enfants perdus — Guillard Cyriac | 9791094757819 | Reçu par la BnF | **présent** (1 notice) |
| 05/11/2026 | Éditions Gallimard | Les enfants perdus : Les aventures de Thomas … — Sureau François | 9782073143068 | Annoncé éditeur | absent (0 notice) |
| 06/11/2026 | Le diplodocus | Dodo là — Letuffe Anne | 9791094908624 | Annoncé éditeur | absent (0 notice) |

## Ce que la mesure change pour `add`

Aujourd'hui, pour 33 des 40 déclarations parues dans les 90 jours, le catalogue BNF
ne rend rien. Le refus pour absence de notice est correct, mais il ferme la porte à
un livre qui existe et se vend. La déclaration porte pourtant de quoi l'annoncer :
titre, auteur, éditeur, collection, date de parution, ISBN-13 contrôlé, prix TTC,
pages et format.

Elle ne le porte pas toujours, et c'est ce qui borne le lot :

- **2 déclarations sur 40 n'ont aucun ISBN exploitable.** Une déclaration sans code
  valide ne peut pas fonder l'identité, et ne doit pas en inventer une.
- **L'état de dépôt ne remplace pas le catalogue.** « Reçu par la BnF » décrit un
  dépôt, pas une notice : la provenance doit être dite telle quelle, jamais comme
  une confirmation de catalogage.
- **La date déclarée peut être future** (2 témoins, « À Paraître »). C'est un motif
  de refus à part entière — « parution annoncée, pas encore parue » —, distinct
  d'une absence de catalogue.

Ce que la mesure ne dit pas, et que le lot ne prétend pas : qu'une déclaration soit
une notice bibliographique. Elle est une annonce d'éditeur ; la BNF reste la seule
autorité d'identité, et une déclaration ne renverse jamais une notice du catalogue.

## Limites

- **L'absence est établie par ISBN, pas par œuvre.** Un contrôle complémentaire
  borné, par titre, sur 6 déclarations absentes par ISBN, a rendu 0 notice pour 4
  d'entre elles et plusieurs pour 2 autres ; mais `bib.title all` est un ET sur les
  mots du titre (« Le silence des blessures » : 8 notices, aucune portant l'ISBN
  déclaré). L'existence de l'œuvre sous un autre ISBN n'est donc pas établie. Un
  livre absent par son ISBN peut être catalogué sous un autre code, ou sous une
  autre édition.
- **L'échantillon penche vers les petits éditeurs et l'auto-édition**, parce que la
  liste par date est exhaustive et qu'elle en est dominée (Books on Demand, Librinova,
  le Lys bleu, auteurs-éditeurs). Ce n'est pas la structure du flux commercial : sur
  les 8 déclarations d'éditeurs de BD, 3 sont au catalogue. Le taux de 13 % est donc
  un taux de corpus, pas une propriété du catalogue.
- **Une page de grille par date**, première page seulement : le corpus est un
  échantillon des parutions du jour, pas leur total.
- **La BD est déduite de l'éditeur**, jamais déclarée par le site : aucun champ de
  genre n'existe sur la fiche.
- **Un premier passage a rendu des compteurs vides** : le SRU expose
  `<srw:numberOfRecords>` (préfixé), et une lecture non préfixée rend `None` sans
  échouer. Les 45 interrogations ont été rejouées avec la lecture correcte ; le
  verdict de présence, lui, venait des `dc:identifier` et n'était pas affecté. Le
  second passage est aussi un contrôle de reproductibilité : mêmes verdicts.
- **Le prix est celui que l'éditeur déclare**, sans contrôle ni conversion ; il
  n'est pas utilisé par ce lot.
- **La mesure vaut au 30 octobre 2026.** Le site publie des annonces : son contenu
  bouge, et une déclaration peut disparaître ou apparaître sans préavis.
- Le temps de réponse de `add` avec cette source n'est pas mesuré ici : trois
  requêtes HTTP séquentielles s'ajoutent, et seulement quand la BNF, OpenLibrary et
  Google Books n'ont rien confirmé.

## Conditions d'usage de la source

Les conditions d'utilisation de la BnF réservent la réutilisation substantielle de
cette base à une autorisation préalable. Le lot ne fait donc que des **consultations
ponctuelles**, une par livre demandé : pas de moissonnage, pas de flux RSS, pas de
parcours de listes, pas de pagination, pas de cache de la base, trois requêtes HTTP
au plus par recherche, et une seule fiche lue. Une recherche n'est lancée que si les
autres sources n'ont rien confirmé, ce qui borne mécaniquement le volume.

## Validation du lot

La suite automatisée couvre, avec un transport HTTP simulé et sans aucun accès
réseau : la déclaration trouvée par ISBN ; la déclaration trouvée par titre avec
l'auteur concordant ; le refus sur auteur différent ; le refus sur parution future ;
le refus sur ISBN invalide ; le refus quand la déclaration nomme un autre éditeur ;
la source indisponible, qui laisse le comportement d'aujourd'hui intact ; et **zéro
appel à Nouveautés Éditeurs quand la BNF confirme** — l'appel est alors une erreur
du test, pas une tolérance. Les résultats de `ruff check`, `mypy app` et `pytest`
sont reportés dans la revue du lot.

La règle retenue à partir de cette mesure est l'[ADR-055](../decisions/catalogue-market.md#adr-055).
