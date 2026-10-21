# Recherche CAL : diagnostic ciblé du 19 octobre 2026

Statut : corrections locales sur `fix/cal-search-results`, base `7762341`.
Aucune livraison, aucun backfill ni changement de prix/liaison/échéance réel.
Le bot reste en fonctionnement normal. Aucun pilote ou dispositif de gel.

## Observations réseau réelles

Le premier diagnostic VPS autorisé pour Jean Doux (9782756079332) a reçu :

- Page de recherche : HTTP 200 HTML, métadonnée `data-hash` présente.
- `search-results` : HTTP 200, `text/plain;charset=utf-8`, zéro octet,
  sans redirection, `Retry-After` ou restriction explicite détectée.
- L'appel `.json()` levait `JSONDecodeError`, classé `invalid_response` ;
  le gestionnaire d'exception ne transmettait pas la réponse à `failed_offers`,
  ce qui expliquait le statut HTTP absent du diagnostic historique.
- La fiche Jean Doux déjà vérifiée a répondu HTTP 200. L'historique a d'abord
  répondu vide, puis la connexion normale a abouti et une réponse JSON est
  arrivée. Aucun minimum actuel n'est attesté : une erreur de l'ancien script
  diagnostique sur un attribut marchand inexistant a interrompu son compte rendu.
  Cette erreur du script temporaire n'est pas un défaut du collecteur applicatif.
  Le diagnostic n'a écrit aucun prix ; la session renouvelée a pu être persistée.

Le complément expressément autorisé s'est limité à deux GET, sans authentification,
suivi de redirection, offre ou historique. La page possède un seul `data-hash`,
dans `div#hash-cont`, correctement choisi par le collecteur. Les paramètres
`h`, `p=1`, `l=1`, `b=d` correspondent au code. La seconde réponse search-results
était HTTP 200 JSON, 3 310 octets, avec `d` de type chaîne. Aucun cookie envoyé
ou posé, aucune redirection ou restriction signalée pendant ce complément.

Le défaut vide est intermittent ; la cause serveur reste inconnue. Ces faits ne
prouvent ni expiration de session, ni blocage d'IP, ni restriction implicite.
Aucun autre appel CAL ne doit être déduit de cette procédure.

Nanbanjin (9782808500487) n'a pas été appelé : après son deuxième échec, les deux
composantes sont en repos jusqu'au 20 octobre 15:46 Europe/Paris, avec réservation
à 16:15. Ce repos interne d'un jour ne constitue pas un Retry-After source.

## Diagnostic direct borné du 19 octobre, 17:52 UTC

Quatre GET ont été exécutés depuis le VPS : fiche, deux relevés d'offres et
historique. Tous ont répondu HTTP 200, sans redirection ni restriction détectée.
L'ISBN de la fiche concorde. La session Redis existante a été réutilisée sans
reconnexion ni écriture de session. Aucun prix, liaison ou échéance métier n'a changé.

Les offres reçues sont Les Libraires neuf 29,95 € + 9 € et 29,95 € + 0 €,
BAL correct 26,95 € + 0 € et BAL neuf 29,95 € + 3 €. Aucune n'est comparable
au périmètre occasion Très bon état+. Quatre groupes sont présents ; le groupe
22 est absent. Ni cette absence, ni sa présence dans d'autres réponses ne prouve
la fin de tous les marchands. Les valeurs des indicateurs tels que `relook` n'ont
pas été conservées : leur signification n'est pas attestée et ils ne servent pas
de certificat de complétude.

L'historique est accessible : 125 points valides dans le périmètre Très bon état+,
365 jours, livraison incluse selon la convention de l'endpoint déjà vérifiée.
Le minimum observé est 9,36 €, point du 2 juillet 2026 à 22:53 UTC.
Cette observation ne valide ni la recherche corrigée ni une écriture/lecture de
ce résultat en production. La fixture d'offres conserve les montants, états et
structure utiles ; groupes et liens y sont synthétiques, les champs non retenus
ont été omis.

## Corrections et limites

Un corps HTTP 200 vide est un échec `empty_response`, jamais une absence d'offre.
Le connecteur conserve statut HTTP, type de contenu et nombre d'octets. Une seule
reprise est possible dans le budget existant, après 1,5 seconde, seulement si les
reprises sont activées et qu'aucun Retry-After n'est présent. À la création d'une
alerte, l'orchestration existante assure cette reprise si le connecteur est en
mode sans reprise interne ; les deux budgets ne se cumulent pas. Une restriction ou
HTTP 429 arrête ce parcours. Un JSON valide avec `d` vide reste distinct.

Lorsqu'une fiche est déjà enregistrée dans la provenance historique ou le cache
CAL, elle est réutilisée avant la recherche. HTTPS, hôte CAL et ISBN normalisé
identique sont obligatoires, sans ISBN contradictoire. Aucun lien n'est construit
à partir d'un titre ou d'un numéro supposé. L'identité de la fiche est encore
contrôlée par le parcours produit existant, notamment ISBN et tome. Seul CAL reçoit
cet indice ; les autres fournisseurs gardent leur interface. Aucun changement de
la politique de fraîcheur, des dates, des critères utilisateur ou du scheduler.
Les ouvrages sans URL vérifiée continuent à utiliser la recherche : cette correction
ne garantit pas la résolution d'une panne serveur persistante.

Une réponse d'offres structurée ne certifie pas à elle seule sa complétude, mais un
signal vérifiable existe : la collecte est complète quand tous les moteurs demandés
ont répondu. Un moteur qui a répondu sans rien trouver est présent avec une liste
vide ; un moteur muet est absent. Le groupe 22 n'est ni nécessaire ni suffisant.
Le relevé se poursuit par un sondage borné, en réutilisant la fiche et ses jetons
déjà obtenus et la liste rétrécissante des moteurs encore attendus, sans régénérer
le `data-hash` ni relancer la recherche. Trois états restent distincts : `complete`
si l'ensemble en attente est vide, `partial` (`incomplete_offers`) si la borne de
tours ou de durée est atteinte, absence confirmée si la collecte est complète sans
offre comparable. Un `partial` n'est jamais converti en absence, et un schéma
invalide reste un échec technique. Les autres fournisseurs ou résultats explicitement
réussis distinguent toujours offre comparable et résultat complet sans offre
comparable. Cette limite est volontairement visible dans le diagnostic interne.

Les observations comparables partielles sont enregistrées en snapshots avec
provenance et statut, sans remplacer les offres validées ni leur prix ou date de
réussite. Leur statut ne bloque pas la réussite indépendante de l'historique.
Une nouvelle fiche peut donc recevoir son minimum historique sans prix courant
validé. Aucun champ ou date de complétude n'est fabriqué.

## Sélection READ ONLY à 18:42 Europe/Paris

273 fiches de livres existent ; 271 sont surveillées par au moins une alerte BD
active ou en pause non supprimée. Ce ne sont pas nécessairement 271 éditions
bibliographiquement certifiées. Les deux anciennes fiches désormais sans liaison
surveillée (Nanbanjin 9782808515023 et Minor Arcana T1 9782413091417) restent conservées.
Aucune association ambiguë n'est corrigée par ce chantier.

Les effectifs suivants se recouvrent :

| Observation sur les 271 fiches surveillées | Nombre |
| --- | ---: |
| Prix courant renseigné en cache, date fraîche ou ancienne | 131 |
| Liste d'offres marchands non vide | 116 |
| Collecte d'offres réussie depuis moins de sept jours | 57 |
| Aucune date prouvée de collecte d'offres réussie | 214 |
| Historique source certifié absent | 271 |
| Historique source récent | 0 |
| Réservation future | 271 |
| Repos après échec sur les composantes | 10 |
| Au moins une composante arrivée à échéance | 261 |
| Sans réservation | 0 |
| Réservation exécutable immédiatement | 0 |

La passe de redémarrage a réservé de petits lots futurs aux créneaux :15/:45,
avec huit livres au maximum par créneau. Elle a donc correctement trouvé zéro
réservation déjà exécutable ; cela ne signifiait pas que les données étaient
complètes. Les réservations observées s'étalent du 19 octobre 18:45 au 20 octobre
16:15. Un test PostgreSQL vérifie qu'au passage suivant le premier lot est sélectionné,
sans inventer une date de réussite ni repousser indéfiniment toutes les réservations.
Les logs réels confirment ensuite huit livres traités à 18:15 puis huit à 18:45,
tous dégradés, sans mise à jour de prix ; le poller reste actif et l'app saine.

Exemples à l'inventaire : Jean Doux, sans réussite prouvée, réservé le 19 octobre
20:15 ; Nanbanjin, sans réussite prouvée, en repos puis réservé le 20 octobre
16:15 ; Birdking Livre 1, dernière réussite d'offres le 16 octobre 15:45 et historique
absent, réservé le 20 octobre 11:15. Son état de dernière tentative `failed` conserve
la vraie date de réussite antérieure : l'échec n'est pas promu en réussite.

## Preuves isolées et suite proposée

La fixture anonymisée `empty_search_results_2026_10_19.json` décrit le vrai corps
vide reçu. Les transports simulés testent reprise bornée, JSON invalide, Retry-After,
URL d'édition, restrictions et absence de recherche lorsque la fiche est connue.
La recette PostgreSQL teste collecteur, offres, frais/états, historique source,
stockage/relecture/rendu, conservation après HTTP 429 et sélection du prochain lot.
Le minimum 9,36 € y vient uniquement de la fixture source antérieure du 18 octobre ;
il n'est ni une constante applicative ni une observation réseau du jour.

Deux captures navigateur du 21 octobre 2026 fondent le critère de complétude :
Jean Doux reçoit ses 32 moteurs au premier relevé, sans sondage ; Nanbanjin en reçoit
27 puis les 5 restants après trois sondages, la liste des moteurs attendus passant
de `0_1_22_30_31` à `1_30_31` puis `30_31`. Les réponses rejouées sont conservées
dans `lookup_jean_doux_2026_10_21.json` et `lookup_nanbanjin_2026_10_21.json` :
montants, états et groupes y sont réels, les liens d'affiliation et les jetons de
session ont été retirés. Jean Doux y porte 15 offres retenues, la meilleure à
16,28 € livrée ; Nanbanjin est complet sans offre comparable. Ces deux livres ne
mesurent pas un taux de réussite : ils démontrent un protocole.

Après validation et autorisation de livraison au SHA exact : commencer par un
petit lot d'éditions à identité démontrée et sans repos actif, réutiliser les données
récentes et vérifier prix, offres et dates par composante. Contrôler les associations
ISBN et chaque tome ; laisser les ambiguïtés à part. Une validation réseau actuelle
complète, puis toute écriture réelle ciblée, nécessitent leurs mandats respectifs.
Une reprise globale par petits lots demandera ensuite un inventaire actualisé,
son périmètre et une autorisation distincte. Aucune reprise n'est exécutée ici.
