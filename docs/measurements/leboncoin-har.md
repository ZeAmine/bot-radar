# Leboncoin — lecture du HAR du 20 octobre 2026

Statut : observation datée d'une session navigateur connectée. Aucune valeur de
cookie, de clé ou de compte n'est conservée ici ; le fichier HAR reste hors du dépôt.

## Source

- HAR fourni par l'utilisateur, 2,9 Mo, 27 entrées, toutes `fetch`/XHR : l'export ne
  contient pas la requête de document HTML.
- Navigateur : UA Chrome 155 sur macOS, `sec-ch-ua` Brave 155.
- Aucun en-tête `Cookie` dans le fichier : ne pas en conclure que les appels sont anonymes.

## Appel de recherche observé

`POST https://api.leboncoin.fr/finder/search`, `200`, `application/json`.

Corps exact :

```json
{"filters":{"enums":{"ad_type":["offer"],"leisure_book_genre":["comics&mangas"]},"category":{"id":"27"}},"sort_by":"relevance","limit":0}
```

- `limit: 0` renvoie les agrégats sans annonce : `total` 1 256 081 (SSR : 1 256 127),
  `max_pages`, `pivot`, `aggregations`, `total_private`, `total_pro`.
- En-têtes navigateur notables : `api_key`, `x-lbc-experiment`, `origin` et `referer`
  `https://www.leboncoin.fr`, `sec-ch-ua`, `accept: */*`, `content-type: application/json`.
- Réponse : `x-datadome: protected`, `x-amz-cf-id`, `via`, `x-envoy-upstream-service-time`.

## Test anonyme du 20 octobre (via le tunnel résidentiel)

Même corps avec `limit: 35`, puis sans `api_key`, puis avec `offset: 35` :

| Variante | Statut | En-tête |
| --- | --- | --- |
| `limit: 35`, avec clé | `403` | `x-datadome: protected` |
| `limit: 35`, sans clé | `403` | `x-datadome: protected` |
| `offset: 35`, avec clé | `403` | `x-datadome: protected` |

Le corps est `{"url": ...}`, une adresse de défi : elle n'est pas suivie.

**Conclusion** : l'API JSON n'est pas accessible sans la session du navigateur.
Elle reste hors périmètre (aucun compte) ; le parsing SSR reste la source.

## Ce que le HAR ne donne pas

- Pas de JA3/JA4 : le HAR ne transporte pas la poignée de main TLS ; l'empreinte
  relevée séparément reste la référence.
- Pas d'en-têtes de document HTML ; la page reste surveillée par le canari.
- Aucune valeur de cookie, de clé ou d'identifiant de compte n'est citée.

## Conséquence

Décision de l'étape 1 : garder le SSR. `api_key` et `x-lbc-experiment` ne sont pas
nécessaires au SSR, qui répond `200` sans eux.
