# Écarts ouverts et limites de preuve

Statut : suivi ciblé, relu au code `6292e2e` le 14 octobre 2026. Aucun correctif
fonctionnel n'est autorisé par cette page. Ne pas transformer un constat en permission.

| Sujet | État et preuve | Travail distinct / condition de clôture |
| --- | --- | --- |
| C11 — secrets au démarrage | Corrigé localement : `validate_production_secrets` précède la configuration Telegram et les producteurs ; `test_unified_lifespan.py` couvre le refus | Ne prouve ni configuration ni transport de production |
| C13 — coroutine non attendue | La [consolidation](consolidation-validation.md#qualification-et-preuve) décrit deux doubles SQL corrigés et une sélection finale sans avertissement sur sa révision ; pas une preuve pour tout arbre ultérieur | Pour toute nouvelle occurrence : reproduire, identifier le double, corriger et réexécuter ; ne pas masquer l'avertissement |
| Publication → détection | La source Vinted n'expose pas `published_at` ; Leboncoin le fournit depuis l'ADR-052 et la latence y est donc mesurable | Pour Vinted, une source réelle attestée reste nécessaire avant de promettre cette durée ; voir [latence](alert-latency.md) |
| Garde ISBN sans génération dédiée | La garde des anciennes lignes utilise `updated_at` ; collisions/horloge/écritures hors contrat limitent la preuve ABA | Arbitrage de version dédiée si une garantie indépendante de l'horloge est requise ; voir [ISBN](isbn-removal.md) |
| Sauvegarde opérationnelle | Outillage synthétique, sans transport indépendant, planification ou restauration production activés | Mandat d'exploitation séparé ; voir [récupération](backup-recovery.md) et prérequis historiques de livraison |
| Livraison Telegram | Accusé, persistance et réception utilisateur diffèrent ; incertitude après transport et crash | Ne pas promettre exactement une fois ; appliquer [notification-delivery](notification-delivery.md) |
| Collecte Leboncoin — sortie résidentielle | Validée depuis l'hôte VPS (relais résidentiel + tunnel, `200`) ; depuis `radar_app`, aucun port de l'hôte n'est joignable : `INPUT` n'accepte que `22` avant un `REJECT` | Choisir la variante pare-feu ou le conteneur dédié, puis vérifier depuis le conteneur ; voir [déploiement](operations/deployment.md#depuis-le-conteneur-radar_app--étape-supplémentaire-obligatoire) |
| Collecte Leboncoin — cadence | Soak de 48 h sur le flux BD réel, 600 s ± jitter, démarré le 2026-10-20 01:43 (Paris) ; après 10,9 h : 65 cycles, 0 blocage, λ ≈ 83/h en journée, saturation observée à 33 nouveautés sur une page de 35 | Terminer le soak (2026-10-22 01:43) ; la page 2 reste nécessaire en pointe ; voir [mesures](measurements/leboncoin-baseline.md) |
| Collecte Leboncoin — rappel | Aucun oracle externe : l'utilisateur refuse la recherche sauvegardée par e-mail comme l'échantillon manuel (décision du 2026-10-20) ; le rappel n'est donc pas mesurable contre la plateforme | Si un oracle est accepté plus tard, pré-enregistrer l'échantillon ; sans lui, ne revendiquer aucun rappel (ADR-053) |
| Collecte Leboncoin — annonces épinglées | 1 à 2 annonces promues restent en tête de page avec une date ancienne (11–18 octobre observés) ; la fraîcheur les exclut, le dédoublonnage les absorbe | Surveiller leur part si elle dépasse 2 places ; voir [mesures](measurements/leboncoin-baseline.md) |

## Limites historiques remplacées

L'ancienne limite F08 « fenêtre annuelle non revalidée à la lecture » est dépassée :
`reference_price()` revalide la projection annuelle, ses dates et sa provenance.
Les détails sont dans [le contrat marché](contracts/catalogue-market.md). Cela ne
prouve ni exhaustivité des relevés ni collecte actuelle d'un fournisseur.

Les anciennes affirmations d'enums dupliqués, de second matcher ORM, de POST IA
parallèle ou de SQL déjà extrait ne sont pas des tickets ouverts sans reproduction.
Les décisions conservées dans [l'index ADR](decisions/README.md) expliquent les raisons.
La suppression historique ciblée des tests d'outillage d'autonomie est consignée
dans [la consolidation](consolidation-validation.md) ; elle n'autorise aucune autre suppression.
