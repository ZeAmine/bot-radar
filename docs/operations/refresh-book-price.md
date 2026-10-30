# Relancer la cote d'un livre nommé

Statut : geste d'exploitation ponctuel, exécuté à la main, sous les plafonds de la
source. [Contrat marché](../contracts/catalogue-market.md).

Le rafraîchissement ordinaire est planifié et une cote fraîche n'est pas
réinterrogée : c'est la garde de fraîcheur qui protège la source. Ce geste existe
pour le cas inverse — un livre nommé dont l'état de rafraîchissement est faux,
daté par une passe qui ne l'a jamais interrogé, et qu'aucun bouton de
l'application ne relance avant l'échéance.

```sh
python scripts/refresh_book_prices.py --isbn 9782857085737 --isbn 9782080270290
```

Le script n'écrit aucune valeur à la main : il appelle `price_book`, la collecte
partagée qui décide, persiste et replanifie — mêmes décisions, mêmes traces, même
historique que le bouton « Prix du marché ».

Limites, celles de la source : deux livres par fenêtre de quinze minutes, douze
requêtes par collecte, une collecte à la fois, deux secondes entre deux requêtes.
C'est le régulateur CAL qui les applique. Un refus est rapporté tel quel
(`deferred`), jamais contourné par une nouvelle tentative immédiate ; un défi
anti-robot arrête la série et se traite comme un incident de source, pas comme
une panne applicative.

La sortie est une ligne JSON par livre — état de cote retenu, montant et marchand,
plus bas sur un an, tentatives réelles — puis un récapitulatif. Le code de sortie
vaut 1 si un livre n'a pas rendu d'état de cote exploitable, ce qui rend l'échec
visible à un enchaînement de commandes.
