# Leboncoin — mesures de référence du 20 octobre 2026

Statut : mesures datées, une seule session nocturne, aucune preuve de production.
Elles bornent la conception, elles ne l'attestent pas. Décision liée : [ADR-053](../decisions/marketplace.md#adr-053).

## Méthode

- Client : `curl_cffi` 0.16.3, `impersonate="chrome"`, depuis le VPS, à travers le tunnel SSH (`socks5://127.0.0.1:1080`) et le relais du Raspberry Pi : sortie résidentielle.
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

## Ce que ces mesures invalident

- « la page est triée par nouveauté » : vrai après 1 à 2 annonces épinglées, à exclure du calcul de flux ;
- « `total` est la catégorie entière » : faux, il suit le filtre ;
- « la page 2 n'existe pas ou recouvre la page 1 » : faux, elle avance ;
- « `delta_total` mesure le flux » : faux, il ne suit pas les arrivées ;
- l'estimation λ de 0,3/h calculée sur l'écart min/max est un artefact des annonces épinglées.

## Limites

- Une session nocturne, un seul jour : ni week-end, ni soirée, ni saisonnalité.
- Aucun oracle externe : le rappel n'est pas mesuré (ADR-053).
- Pas encore de HAR de navigateur ni de canari de schéma.
