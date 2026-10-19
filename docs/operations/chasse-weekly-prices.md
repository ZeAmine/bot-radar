# Prix Chasse aux livres : recette et reprise progressive

Statut : procédure de reprise manuelle par inventaire approuvé, indépendante du
fonctionnement du bot. Aucune reprise massive n'est autorisée par cette page.
Une empreinte contrôle le périmètre ; elle ne remplace pas l'autorisation humaine
d'écriture. [Contrat marché](../contracts/catalogue-market.md).

## Préparer la reprise

1. Faire approuver séparément la livraison du code et la migration additive 0015,
   selon le workflow du dépôt. Elle ajoute provenance, échéances par composante et
   réservation de passage ; elle ne transforme aucun ancien prix en preuve source.
2. Dans l'environnement cible, générer un inventaire en lecture seule :

   ```sh
   python scripts/backfill_chasse_prices.py --manifest /chemin/prive/inventaire.json
   ```

   Le script refuse d'écraser un manifeste existant ; PostgreSQL utilise une
   transaction READ ONLY. Les alertes actives et en pause non supprimées sont
   inventoriées, avec leurs livres distincts et leurs dates de réussite réelles.
   `checked_at`, une date de tentative ou un minimum brut sans provenance ne sont
   jamais promus en collecte historique réussie.
3. Examiner les conflits d'ISBN, les ouvrages absents, les doublons ISBN-10/13 et
   les éditions/langues alternatives. Une similitude de titre seule ne suffit pas
   à remplacer une édition existante. Constituer si nécessaire un plan JSON de
   réparations bibliographiques, chaque entrée comprenant `alert_id`,
   `expected_name`, `expected_isbn`, `expected_book_ids`, `isbn13`, `title`,
   `language` et `evidence_url`. Les autres métadonnées prouvées sont facultatives.
   Regénérer un manifeste avec `--catalogue-plan /chemin/prive/reparations.json`.
4. Présenter les effectifs, changements de liaisons exacts, conservation des anciennes
   données, limites et empreinte du manifeste. Attendre l'autorisation explicite
   avant toute commande `--apply`.

## Exécution bornée après autorisation

```sh
python scripts/backfill_chasse_prices.py \
  --manifest /chemin/prive/inventaire.json \
  --apply --approved-digest EMPREINTE_APPROUVEE --limit 8
```

Le script exige un inventaire et son empreinte approuvée. Chaque invocation
traite un lot borné, séquentiellement, avec les délais et
la politique de reprise du service partagé. Les invocations doivent être espacées
et leurs bilans contrôlés ; ne pas lancer une boucle parallèle de reprises.
Les livres devenus récents sont réutilisés, les échecs restent en repos, les
identités et appartenances sont revalidées. Le manifeste modifié est refusé.

Les réparations bibliographiques exigent que l'alerte et ses anciennes liaisons
correspondent toujours au plan. Elles conservent le statut actif/en pause et les
anciens livres, caches et observations. Les aliases ISBN-10/13 sont reliés au livre
canonique uniquement si leur équivalence est prouvée et dans les alertes approuvées.
Les appels suivants restent idempotents. Les éditions alternatives d'une série ne
sont pas supprimées automatiquement.

Après chaque lot : vérifier les résultats par livre, les nouvelles dates de réussite
par composante, les offres conservées en cas d'échec et les réservations futures.
Regénérer un inventaire READ ONLY pour mesurer les manquants et les conflits restants.
Ne pas conclure à une reprise complète à partir du seul nombre de livres parcourus.
Aucun nouveau cron ni processus worker n'est nécessaire. Le bot reste en service.
Les incidents sont traités par la politique de reprise des collectes et sessions,
sans verrou global ni compteur persistant propre à cette procédure.

## Inventaire réel en lecture seule du 18 octobre 2026

La requête a utilisé une transaction PostgreSQL READ ONLY. Aucun livre, prix,
liaison, statut ou date de production n'a été modifié.

- 142 alertes BD actives ou en pause, 274 livres distincts liés.
- 62 livres possèdent une date explicite de réussite des offres ; aucun des 274
  n'a la nouvelle preuve historique structurée. Les autres dates restent inconnues.
- Deux conflits entre ISBN d'alerte et livre lié : Nanbanjin et Minor Arcana T2.
- Une alerte sans livre : Tout ce qui meurt & agonise.
- Un groupe de doublons ISBN-10/13 : Blacksad, 2205059254 / 9782205059250.
- Les deux tomes Absolute Martian Manhunter sont actuellement liés à l'alerte.
  Leur présence en base ne prouve pas que l'ancien panneau les affichait tous.
  Les séries contiennent aussi plusieurs éditions et langues : elles restent
  visibles, mais aucun choix arbitraire ni total incomplet n'est présenté.

Le manifeste privé préparé est `/private/tmp/chasse-backfill-manifest-2026-10-18.json`,
empreinte `bfeaad56b5dcc644747567e1bb6e8572d653effbdbf9444079dff41af7eba58e`.
Il prépare trois réparations explicites :

| Alerte | Ancienne association | Association proposée | Preuve consultée |
| --- | --- | --- | --- |
| Nanbanjin | 9782808515023 | 9782808500487 | [Dupuis](https://www.dupuis.com/nanbanjin/bd/nanbanjin-nanbanjin/140973) |
| Minor Arcana T2 | 9782413091417 | 9782413091424, tome 2 | [Fnac](https://www.fnac.com/a22731010/Minor-Arcana-Tome-2-Minor-Arcana-T02-Jeff-Lemire) |
| Tout ce qui meurt & agonise | Aucune | 9791026856405 | [Urban Comics](https://www.urban-comics.com/tout-ce-qui-meurt-agonise/) |

La résolution catalogue automatique de Minor Arcana T2 n'a pas retrouvé de notice
confirmée lors de la sonde ; la réparation proposée s'appuie sur la fiche explicite
consultée et exige l'approbation du plan. Les autres associations de séries ne sont
pas certifiées collectivement par cet inventaire. À cette date d'inventaire, aucun des trois changements ni la consolidation ISBN
n'avait été appliqué. Nanbanjin puis Minor Arcana T2 ont été réparés lors des
exécutions suivantes ; leur retrait n'est pas prévu. Tout ce qui meurt & agonise
et la consolidation ISBN restent à traiter sous mandat distinct.

## Preuves et limites

La recette isolée couvre le minimum source, sa persistance après relecture, la carte
de création et le panneau, les échecs/partiels sans perte, les réservations bornées,
la concurrence, la sélection des tomes et l'idempotence des réparations.
Les résultats détaillés et nombres de tests sont ceux du compte rendu de livraison.
Le diagnostic réseau réel est distinct des transports simulés et de la base isolée.
Un résultat local ne valide ni la livraison ni une reprise de production.
