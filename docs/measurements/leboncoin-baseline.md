# Leboncoin — mesures de référence du 20 octobre 2026

Statut : mesures datées, une seule session nocturne, aucune preuve de production.
Elles bornent la conception, elles ne l'attestent pas. Décision liée : [ADR-053](../decisions/marketplace.md#adr-053).

## Méthode

- Client : `curl_cffi` 0.16.3, `impersonate="chrome"`, depuis le VPS, à travers le tunnel SSH
  et le relais du Raspberry Pi : sortie résidentielle. **Montage depuis remplacé** : le
  contrôle se fait désormais depuis le conteneur, en `socks5h://` vers la gateway du
  réseau applicatif (voir [déploiement](../operations/deployment.md)). La présente
  mesure reste l'enregistrement de ce qui a été fait ce soir-là, pas un mode opératoire.
- URL : `https://www.leboncoin.fr/recherche?category=27&leisure_book_genre=comics%26mangas`.
- Heure : 20 octobre 2026, 01:38–01:44 (Europe/Paris).
- Pages capturées : page 1 et page 2, une requête chacune.

## Filtre et pagination

- `category=27` seul : `total` 11 505 808. Avec `leisure_book_genre=comics%26mangas` : `total` 1 256 127. Les valeurs `comics` et `comics-mangas` renvoient 0 : la valeur attendue est bien `comics&mangas`.
- `human_readable_applied_condition` vaut `global_BDC` ; `total_active` et `applied_condition` valent 0 (non renseignés). Ne pas s'appuyer sur `total_active`.
- `total` = `total_private` (1 250 692) + `total_pro` (5 435) : compteur du sous-flux, pas de la catégorie entière.
- `max_pages` = 100. Le paramètre `page=2` renvoie 35 annonces plus anciennes, avec un seul recouvrement (une annonce épinglée) : la pagination avance, le rattrapage est fondé.

## Flux et couverture

- Page 1 : 35 annonces, 2 épinglées (dates du 11 au 18 octobre) puis 33 triées par date décroissante, du 20 octobre 01:37:26 à 00:58:15, soit 39,2 min.
- Page 2 : 33 non épinglées, de 00:57:04 à 00:32:03, soit 25,0 min.
- Les deux pages sont contiguës : 66 annonces non épinglées sur 65,4 min, soit λ ≈ 61 annonces par heure (≈ 1,0 par minute), cohérent avec l'estimation antérieure de 66/h.
- À 10 min : ≈ 10 annonces par cycle pour 35 places, soit 3,5 fois de marge ; deux pages couvrent ≈ 70 min.
- Débordement (plus de 70 arrivées en 10 min) : il faudrait λ ≈ 420/h, soit ≈ 7 fois le flux observé.
- `total` n'a pas varié entre 01:38 et 01:43 alors que des annonces arrivaient : le compteur peut être retardé ou compensé par les suppressions. Le soak mesure `delta_total` et `new_ids` à chaque cycle pour trancher.

## Annonces épinglées

La tête de page contient 1 à 2 annonces promues, plus anciennes, qui occupent des places et changent d'une requête à l'autre. La règle `published_at > alert.created_at` les exclut ; le dédoublonnage `(source_name, external_id)` les absorbe si elles reviennent.

## Empreinte TLS

Relevé via `https://tls.peet.ws/api/all`, même client, même sortie :

| Élément | Valeur |
| --- | --- |
| JA3 | `06648eba2ce507b1e1b113ec3272599f` |
| JA4 | `t13d1516h2_8daaf6152771_806a8c22fdea` |
| HTTP/2 (Akamai) | `52d84b11737d980aef856699f885ca86` |

Le préfixe JA4 `t13d1516h2_8daaf6152771` est la forme Chrome attendue. La comparaison avec un HAR de navigateur réel reste à faire.

## Soak du 20 octobre — 10,9 h

- 65 cycles, 65 réponses `200`, aucun captcha, aucune erreur, requête la plus lente à 3,2 s.
- `new_recent_ids` (ids nouveaux dont `first_publication_date` suit le cycle précédent) : moyenne 13,8 par cycle, maximum 33, neuf cycles sans nouveauté. Soit λ ≈ 83 annonces/heure en journée, contre 61/h la nuit : la page de 35 places n'a plus que 2,5 fois de marge en moyenne et frôle la saturation en pointe.
- En journée, la page est saturée : à 12 h, 33 nouveautés par cycle, soit le plafond de la page (35 places moins les épinglées). λ réel est donc censuré au-delà de 33 par cycle ; la page 2 n'est pas une sécurité mais une pièce du régime de pointe, et la fenêtre de page 1 tombe à 10 min vers midi.
- `delta_total` : moyenne −1,0, minimum −53, maximum +54, corrélation 0,39 avec les nouveaux ids. **L'estimateur Δ`total` est invalide** : les suppressions compensent les arrivées et le compteur peut être retardé. Seul l'ensemble des ids datés reste fiable.
- `gone_ids` : 1 164 sur la période, dont une majorité de départs de page 1 provoqués par les arrivées, pas seulement des suppressions.

## Premier blocage observé

- 20 octobre 2026, 10:58:48 UTC (12:58 Paris) : `403` avec page de challenge (`captcha-delivery`), après 65 cycles et environ 11 h à 600 s.
- Deuxième cycle consécutif à 11:09:55 UTC, également `403`.
- Les tests d'API du même jour commencent à 11:16 UTC : ils suivent le premier refus et ne l'expliquent pas.
- Depuis 11:13 UTC, le soak applique le retrait progressif du collecteur (30, 60 puis 120 min) ; les cycles bloqués ne comptent ni arrivées ni couverture.
- Conséquence : la borne « aucun échec sur 288 cycles » ne s'applique plus. Le taux observé est de 2 cycles bloqués sur 65 (≈ 3 %).

### Retour à la normale

- 20 octobre, 11:43 UTC : le soak reprend après le retrait de 30 min, sans nouveau `403`.
- 22 octobre, 11:29 Paris (09:29 UTC) : canari exécuté depuis le clone VPS via le tunnel — `ok`, `200`, 1,19 Mo. Le blocage a duré moins de 45 min ; il est confiné à l'IP résidentielle et se dissipe seul, ce que le retrait progressif du collecteur exploite correctement.

## Panne du tunnel résidentiel — 45 h sans collecte

Le soak a perdu le tunnel le 20 octobre vers 11:43 UTC et ne l'a retrouvé que le 22 à 09:28 UTC :

- 240 `ConnectionError` et 2 `Timeout` sur 339 cycles ; 94 seulement ont vu un `200`.
- Sur le Pi, le client `ssh -R` s'est figé sur `Timeout, server <hôte-vps> not responding` et **la boucle `@reboot` qui devait le relancer avait disparu** : plus aucun processus, aucune tentative de reconnexion journalisée.
- Le relais SOCKS5 du Pi était intact et le VPS joignable : la défaillance est celle du superviseur, pas du réseau.

**C'est le mode de défaillance le plus dangereux documenté à ce jour** : un tunnel à demi-ouvert ne produit ni exception claire ni alerte, seulement une collecte muette. Correctif appliqué : veille cron de 5 min sur le Pi, qui relance la boucle détachée si aucun client `ssh -R` n'existe. Le soak est reparti le 22 octobre à 11:29 Paris.

## Ce que ces mesures invalident

- « la page est triée par nouveauté » : vrai après 1 à 2 annonces épinglées, à exclure du calcul de flux ;
- « `total` est la catégorie entière » : faux, il suit le filtre ;
- « la page 2 n'existe pas ou recouvre la page 1 » : faux, elle avance ;
- « `delta_total` mesure le flux » : faux, il ne suit pas les arrivées ;
- « l'API JSON est une voie anonyme » : faux, `403` sans session du navigateur ;
- « aucune restriction pendant 48 h » : faux, un challenge DataDome est survenu après 11 h ;
- « un soak de 48 h prouve la cadence » : faux, la panne du tunnel l'a interrompu après 11,4 h ;
- l'estimation λ de 0,3/h calculée sur l'écart min/max est un artefact des annonces épinglées.

## Limites

- Une session nocturne, un seul jour : ni week-end, ni soirée, ni saisonnalité.
- Aucun oracle externe : le rappel n'est pas mesuré (ADR-053).
- Une seule panne de tunnel observée sur une seule nuit : sa fréquence n'est pas connue.
