# Écarts ouverts et limites de preuve

Statut : suivi ciblé, relu au code `6292e2e` le 14 octobre 2026. Aucun correctif
fonctionnel n'est autorisé par cette page. Ne pas transformer un constat en permission.

| Sujet | État et preuve | Travail distinct / condition de clôture |
| --- | --- | --- |
| C11 — secrets au démarrage | Corrigé localement : `validate_production_secrets` précède la configuration Telegram et les producteurs ; `test_unified_lifespan.py` couvre le refus | Ne prouve ni configuration ni transport de production |
| C13 — coroutine non attendue | La [consolidation](consolidation-validation.md#qualification-et-preuve) décrit deux doubles SQL corrigés et une sélection finale sans avertissement sur sa révision ; pas une preuve pour tout arbre ultérieur | Pour toute nouvelle occurrence : reproduire, identifier le double, corriger et réexécuter ; ne pas masquer l'avertissement |
| Publication → détection | La sonde historique du catalogue n'exposait pas `published_at` ; les mesures locales commencent à la réception/détection | Une source réelle attestée est nécessaire avant de promettre cette durée ; voir [latence](alert-latency.md) |
| Garde ISBN sans génération dédiée | La garde des anciennes lignes utilise `updated_at` ; collisions/horloge/écritures hors contrat limitent la preuve ABA | Arbitrage de version dédiée si une garantie indépendante de l'horloge est requise ; voir [ISBN](isbn-removal.md) |
| Sauvegarde opérationnelle | Outillage synthétique, sans transport indépendant, planification ou restauration production activés | Mandat d'exploitation séparé ; voir [récupération](backup-recovery.md) et prérequis historiques de livraison |
| Livraison Telegram | Accusé, persistance et réception utilisateur diffèrent ; incertitude après transport et crash | Ne pas promettre exactement une fois ; appliquer [notification-delivery](notification-delivery.md) |

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
