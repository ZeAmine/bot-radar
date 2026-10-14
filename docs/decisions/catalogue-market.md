# Décisions — catalogue-market

Statut : décisions datées ; vérifier le statut de chaque entrée.
Périmètre : contexte et raisons de ce thème, sans procédure d’exploitation implicite.
Contrat/code de référence : [lecture spécialisée](../contracts/catalogue-market.md).

<a id="adr-010"></a>

## ADR-010 : Gestion tolérante des séries BD spéciales (ex: 1629, RIP, Blast)

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Titres courts, chiffres, livres et intégrales ne suivent pas une numérotation universelle.
- Décision et raison : Extraire déterministement les numéros attestés sans imposer une série continue.
- Conséquences : Amendé par ADR-016/F08 et ADR-044 : composition inconnue = comparaison indisponible, jamais total de série substitué à un lot.

<a id="adr-014"></a>

## ADR-014 : La BNF est la seule autorité ISBN (« l'IA propose, la BNF dispose »)

Introduction Git : 2026-10-02 · Statut : **accepté**. Première déclaration : `1caa522600e6`.

- Contexte : Un ISBN proposé par l’IA pouvait attribuer une œuvre à un autre ouvrage.
- Décision et raison : La BNF est l’autorité primaire d’identité ; contrôler titre, auteur et ISBN avant confirmation, et ancrer les propositions dans BookContext.
- Conséquences : Amendé par ADR-033/040/045 : replis secondaires déclarés, non confirmants et sans écraser une contradiction BNF. Le titre historique « seule autorité » n’exclut pas ces replis.

<a id="adr-016"></a>

## ADR-016 : Un seul filtre d'occasion, une seule référence de prix

Introduction Git : 2026-10-02 · Statut : **accepté**. Première déclaration : `1caa522600e6`.

- Contexte : Les filtres d’état et les références divergeaient entre collecte, résumé et panneau.
- Décision et raison : Partager les règles d’occasion et la référence : minimum annuel admissible puis offre courante valide, comparaison au total livré. Une composition de lot doit être complète, non ambiguë et entièrement cotée.
- Conséquences : F08 remplace le cumul partiel et invalide les totaux de série lors des vraies mutations. ADR-035 précise la preuve ISBN. La limite annuelle de F08 est historique : la projection actuelle est revalidée à la lecture ; voir le contrat marché.

<a id="adr-017"></a>

## ADR-017 : Choisir son édition dans le récap, avant de créer l'alerte

Introduction Git : 2026-10-02 · Statut : **accepté**. Première déclaration : `1caa522600e6`.

- Contexte : Choisir automatiquement une édition pouvait engager la veille sur le mauvais livre.
- Décision et raison : Présenter les éditions exploitables au récap avant création et revalider le choix.
- Conséquences : L’ISBN pilote matching, recherche et cote ; œuvres homonymes avant éditions (ADR-021), carte unique (ADR-032).

<a id="adr-019"></a>

## ADR-019 : Une cote « prix le plus bas constaté » bornée par nos propres relevés

Introduction Git : 2026-10-04 · Statut : **accepté**. Première déclaration : `245e42ee22f4`.

- Contexte : Un minimum tiers plus haut qu’un prix observé et des redatations sans collecte donnaient une référence trompeuse.
- Décision et raison : Fonder le « prix bas constaté » sur des observations comparables et garder la date de l’observation, pas celle d’une tentative.
- Conséquences : La projection annuelle actuelle remplace le plancher monotone historique : fenêtre locale revalidée, provenance et snapshot requis. Ni exhaustivité du marché ni fraîcheur d’un fournisseur garanties.

<a id="adr-020"></a>

## ADR-020 : Une cote qu'on ne nourrit plus se signale, et la session se réinjecte

Introduction Git : 2026-10-04 · Statut : **accepté**. Première déclaration : `8a8bb8d5b52c`.

- Contexte : Une valeur conservée sans nouvelle collecte masquait la panne de sa source.
- Décision et raison : Signaler l’absence de renouvellement et permettre une réinjection de session validée, sans afficher le secret.
- Conséquences : Les compteurs et sondes doivent porter sur l’état observé. Renouvellement autonome précisé par ADR-034 puis ADR-036 ; pas une promesse de collecte actuelle.

<a id="adr-021"></a>

## ADR-021 : Un homonyme n'est pas l'œuvre, et changer d'ISBN change d'ouvrage

Introduction Git : 2026-10-04 · Statut : **accepté**. Première déclaration : `9db1823f0807`.

- Contexte : Les homonymes mélangeaient auteurs et éditions ; un changement ISBN gardait des métadonnées incohérentes.
- Décision et raison : Choisir d’abord l’œuvre, puis ses éditions ; réaligner la fiche d’identité lors d’une correction ISBN.
- Conséquences : ADR-038/044 et F06 bornent confirmation, panne, critères manuels et résultats tardifs. Aucun auteur ou éditeur inventé depuis un candidat ambigu.

<a id="adr-023"></a>

## ADR-023 : Une alerte BD ne part jamais sans bloc de cote

Introduction Git : 2026-10-05 · Statut : **accepté**. Première déclaration : `7a3211022fc4`.

- Contexte : Une BD sans prix arrivait sans explication de l’état de sa cote.
- Décision et raison : Toujours rendre un bloc de cote ou une ligne d’absence ; programmer le rattrapage après l’envoi.
- Conséquences : Amendé par ADR-039/041/044 : absence d’offre, catalogue épuisé, lot inconnu et indisponibilité à l’envoi sont distincts ; aucun message ancien promis à une mise à jour.

<a id="adr-025"></a>

## ADR-025 : Une œuvre numérotée est une série, pas un tome

Introduction Git : 2026-10-05 · Statut : **accepté**. Première déclaration : `504aa51a84a6`.

- Contexte : La résolution prenait un tome trouvé pour toute l’œuvre demandée.
- Décision et raison : Détecter une série depuis les titres numérotés réellement rendus lorsque la demande ne vise pas un tome ; surveiller la série sans ISBN de volume.
- Conséquences : Pas de liste figée d’œuvres. La preuve peut venir d’une notice voisine non retenue comme édition ; présentation simplifiée par ADR-031.

<a id="adr-026"></a>

## ADR-026 : Une série se compose par ses tomes, et la mesure se fait à l'envoi

Introduction Git : 2026-10-05 · Statut : **accepté**. Première déclaration : `19c67d6397b1`.

- Contexte : Une seule fiche ne couvrait pas une série ; un diagnostic au repos masquait les cotes réellement envoyées.
- Décision et raison : Lier et coter les tomes identifiés ; compter l’état de cote au moment de l’envoi.
- Conséquences : Un compteur nul avant toute mesure n’est pas un succès. Composition complétée par ADR-040 ; limite de lot complet imposée par ADR-016/F08.

<a id="adr-028"></a>

## ADR-028 : L'identifiant se lit, il ne se devine pas — et le récap dit quand il manque

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `d8eb72af7eb0`.

- Contexte : Le parseur perdait des identifiants d’autorité et le récap taisait leur absence.
- Décision et raison : Lire les formes ISBN/URN/EAN livre avec clé valide, sans deviner depuis une URL ; afficher l’absence d’identité.
- Conséquences : ADR-031 supprime la ligne ISBN pour une série volontairement sans code ; ADR-041/045 évitent d’attribuer une panne ou un manque de notice à une source non consultée.

<a id="adr-032"></a>

## ADR-032 : Le récap de création et la confirmation sont une seule carte

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `0c6982ef3b4a`.

- Contexte : Récap et confirmation dérivaient parce qu’ils reconstruisaient la même identité.
- Décision et raison : Partager creation_card et creation_keyboard, avec en-tête/pied et bloc de cote propres au contexte.
- Conséquences : Les alternatives d’édition n’apparaissent qu’à partir de deux éditions exploitables ; la carte conserve les choix confirmés.

<a id="adr-033"></a>

## ADR-033 : Une troisième autorité, et l'aveu en tête de carte

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `9bb01069eda3`.

- Contexte : Certaines nouveautés manquaient dans les deux premières autorités.
- Décision et raison : Ajouter Google Books configuré après BNF puis OpenLibrary, provenance déclarée et confiance secondaire.
- Conséquences : Amende ADR-014 : aucun repli ne confirme à la place d’une BNF contradictoire ; aucune source non interrogée annoncée.

<a id="adr-034"></a>

## ADR-034 : Le jeton Chasse se renouvelle seul, sur l'hôte, et un doute déclenche la visite

Introduction Git : 2026-10-06 · Statut : **remplacé pour la capture navigateur par ADR-036 ; garde opérationnelle conservée**. Première déclaration : `01e675ac995b`.

- Contexte : L’historique cessait de se renouveler sans reprise manuelle de session.
- Décision et raison : Prévoir un renouvellement sur l’hôte, conditionné à l’âge de la dernière observation certifiée ; un résultat illisible déclenche la tentative.
- Conséquences : Le mécanisme navigateur est remplacé par ADR-036. Restent le verrou, les délais, les codes d’échec et la visibilité des cotes figées ; pas de permission d’installer un cron ici.

<a id="adr-035"></a>

## ADR-035 : L'ISBN exact prouve l'identité, la graphie du titre ne vétote plus

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `413fa36783da`.

- Contexte : Une graphie de titre pouvait rejeter le bon ISBN explicitement affiché.
- Décision et raison : L’égalité exacte d’un ISBN valide attendu et affiché prouve l’identité et court-circuite le veto de titre.
- Conséquences : Sans cette preuve, le veto d’ADR-016 et son seuil restent inchangés. Une autre édition n’est pas un ISBN égal.

<a id="adr-036"></a>

## ADR-036 : Le jeton Chasse vient du compte, pas d'un navigateur

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `a1968dda5c8c`.

- Contexte : Le renouvellement navigateur d’ADR-034 rencontrait un défi images.
- Décision et raison : Remplacer ce mécanisme par la connexion membre puis validation/injection existante du jeton ; ne pas résoudre le CAPTCHA.
- Conséquences : Remplace la capture Playwright/Xvfb d’ADR-034, conserve son contrat d’échec. Identifiants hors dépôt, fichiers privés, aucune URL contenant le jeton en log ; constat de fonctionnement ancien, pas preuve actuelle.

<a id="adr-037"></a>

## ADR-037 : Le total de série s'écrit à chaque cote, et chaque tome a sa ligne

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `d4e1c62d9ff2`.

- Contexte : Des tomes sans prix disparaissaient et le total de série variait selon l’écran.
- Décision et raison : Recalculer le total dans le chemin central après écriture de cote ; afficher aussi les tomes sans prix et permettre leur navigation.
- Conséquences : F08 borne validité et invalidation des totaux. Un total de série partiel n’est jamais une référence complète de lot ; ne pas refaire une somme en lecture.

<a id="adr-038"></a>

## ADR-038 : Telegram consomme le contrat partagé, pour les nouvelles créations seulement

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `cef6aa81df33`.

- Contexte : La création manuelle et naturelle interprétaient différemment les mêmes autorités.
- Décision et raison : Les nouvelles créations consomment BookSearchUseCase et une projection commune : trouvé confirmé, ambigu, partiel, introuvable et indisponible restent distincts.
- Conséquences : L’édition existante conserve l’exception d’ISBN volontaire en panne. Un candidat partiel ne nourrit pas les champs confirmés ou BookContext ; ne pas généraliser cette exception aux nouvelles créations.

<a id="adr-039"></a>

## ADR-039 : Une passe qui n'a rien trouvé a un résultat, et il se dit

Introduction Git : 2026-10-07 · Statut : **accepté**. Première déclaration : `827a57425cf1`.

- Contexte : Une collecte vide était décrite comme une recherche éternellement en cours.
- Décision et raison : Partager cote_absence_line : un relevé vide est une absence d’offre datée, pas une attente.
- Conséquences : ADR-042/044 distinguent collecte échouée, tentative sans relevé et instantané envoyé ; le panneau peut collecter sans promettre de réécrire une ancienne alerte.

<a id="adr-040"></a>

## ADR-040 : Une série se complète par une seconde autorité, et rattrape ses tomes manquants

Introduction Git : 2026-10-07 · Statut : **accepté**. Première déclaration : `5f56ea5de7e1`.

- Contexte : La BNF pouvait laisser une série incomplète et ses volumes manquants sans reprise.
- Décision et raison : Compléter par OpenLibrary sous plafond de 12, sans remplacer une notice BNF, et relancer la résolution de série après envoi si nécessaire.
- Conséquences : Numéros et provenance explicites. Titres et ISBN partent ensemble à la cotation ; pas de collecte pré-Telegram ni d’inventaire non borné.

<a id="adr-041"></a>

## ADR-041 : Une série que personne ne catalogue a une réponse, et elle se dit

Introduction Git : 2026-10-07 · Statut : **accepté**. Première déclaration : `ecbbc9a7815a`.

- Contexte : Une liste vide mélangeait absence d’œuvre et panne de catalogue.
- Décision et raison : Porter consulted, unavailable et notices dans un verdict ; conclure à l’épuisement seulement si au moins une autorité a répondu, aucune n’a échoué et aucune notice n’existe.
- Conséquences : Persister la date du verdict concluant, effacer l’épuisement après découverte. Un silence n’est jamais une preuve d’absence ; état distinct jusque dans l’audit d’envoi.

<a id="adr-042"></a>

## ADR-042 : Le marché se consulte à la demande, une tentative n'est pas un relevé

Introduction Git : 2026-10-07 · Statut : **accepté**. Première déclaration : `836b3d5462d4`.

- Contexte : Les panneaux et tentatives de collecte mélangeaient état actuel et ancienne observation.
- Décision et raison : Partager le panneau annonce/menu et la cotation par livre ; conserver sélection et fermeture, état de tentative distinct des observations et erreurs de fournisseurs.
- Conséquences : Un cache conservé n’est pas redaté sans observation. Faux positif : édition du clavier seulement, persistance idempotente. Un échec photo n’établit pas la cause CDN ; pas de normalisation URL ou téléversement inventé.

<a id="adr-043"></a>

## ADR-043 : La création prépare les cotes, le panneau marché les présente sans doublons

Date : 2026-10-07 · Statut : **accepté**. Première déclaration : `1f6b9d12f065`.

- Contexte : Une nouvelle veille devenait active avant sa préparation catalogue et prix.
- Décision et raison : Persister en pause, terminer le brouillon avant attentes, attendre le chemin central puis relire les prix dans la session de création avant activation.
- Conséquences : Échec sans prix = indisponible ; aucune garantie de cote. ADR-046 ajoute l’intention durable. Les numéros de volumes peuvent être précisés par l’édition du même ISBN.

<a id="adr-044"></a>

## ADR-044 : Changer d’édition réaligne la fiche ; une annonce décrit la cote à l’envoi

Introduction Git : 2026-10-08 · Statut : **accepté**. Première déclaration : `4eed61f7f124`.

- Contexte : Changer d’édition conservait une ancienne identité et les annonces promettaient une cote future.
- Décision et raison : Revalider l’édition, confirmer les identités incertaines, réaligner la fiche et préparer la cote pendant la pause ; une intégrale attestée peut être un livre unique.
- Conséquences : F06 amende la remise à zéro des critères : conserver les critères manuels et retirer seulement l’ancien ISBN exact. Les gardes empêchent les résultats tardifs ; les annonces rendent l’état à l’envoi.

<a id="adr-045"></a>

## ADR-045 : Les homonymes ne doivent pas masquer les replis d'une nouveauté

Introduction Git : 2026-10-08 · Statut : **accepté**. Première déclaration : `b3b9ba4bc530`.

- Contexte : Des homonymes BNF masquaient les replis concordant avec l’auteur explicitement demandé.
- Décision et raison : Consulter les replis dans l’ordre si tous les candidats BNF ont un auteur connu contradictoire ; garder les homonymes distincts des éditions du repli.
- Conséquences : Sans indice, auteur inconnu ou concordant, l’arrêt existant demeure. L’évaluation de 16 titres est historique et liée ci-dessous ; elle ne prouve pas la couverture générale.
- Preuve historique : [évaluation du 8 octobre](../evaluations/recent-books-2026-10-08.md), référence `4eed61f7f124de764173050ce106ef52dbf08ace`, 16 titres/48 appels publics ; aucun appel rejoué ici.

<a id="adr-046"></a>

## ADR-046 : La préparation BD porte une intention durable et une version

Date : 2026-10-08 · Statut : **accepté**. Première déclaration : `3c742f3311e7`.

- Contexte : La pause seule ne portait ni version de préparation ni intention de reprise.
- Décision et raison : Persister une demande nullable avec jeton et intention, transactions courtes sous garde de version, reprise bornée par le scheduler unique.
- Conséquences : Les anciennes pauses NULL ne sont pas reclassées. Une erreur blocked attend un geste ; l’annulation d’un appelant ne tue pas le travail partagé. Retour vers ancien /resume : maintenance obligatoire, sans downgrade destructeur.
- Procédure sensible et limites : [préparation BD](../bd-preparation.md).
