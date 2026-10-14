# Décisions — marketplace

Statut : décisions datées ; vérifier le statut de chaque entrée.
Périmètre : contexte et raisons de ce thème, sans procédure d’exploitation implicite.
Contrat/code de référence : [lecture spécialisée](../vinted-checkout.md).

<a id="adr-012"></a>

## ADR-012 : Migration vers l'API interne JSON Vinted (`svc-catalogue`) & Cadence Adaptative

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Les catalogues HTML lourds et les visites individuelles coûtaient réseau et délai.
- Décision et raison : Interroger le catalogue JSON avec repli HTML, couverture intégrée et cadence adaptative entrelacée.
- Conséquences : Évolution du 13 octobre : intervalle standard 12 s, paramètre servant de plafond aux accélérations. La sonde historique n’exposait pas published_at ; aucun délai publication→détection établi.

<a id="adr-047"></a>

## ADR-047 : Le bouton « Acheter » prépare un checkout à la demande, jamais un paiement

Date : 2026-10-14 · Statut : **accepté**. Première déclaration : `6292e2efcf75`.

- Contexte : L’utilisateur autorise une préparation manuelle accélérée, sans achat autonome.
- Décision et raison : Au clic seulement, vérifier propriétaire et association à une alerte non supprimée, prérequis, session dédiée, verrou anti-double-clic et cache de lien ; préparer la transaction et le checkout.
- Conséquences : Amende ADR-007. Le paiement/relais restent manuels. Échec incertain : verrou conservé ; blocage/limitation/échecs répétés : suspension sans relance automatique. La session validée sur le corps n’est jamais exposée. Le fil vendeur ouvert est un effet réel ; risques contractuels assumés, aucun contournement.
- Observation historique du 14 octobre, source de consolidation `6292e2e` : refus anonyme et défi DataDome classés, suspension observée ; ce ne sont pas une réussite authentifiée ni une observation actuelle. Détails dans [la procédure checkout](../vinted-checkout.md).

<a id="adr-048"></a>

## ADR-048 : Le parcours local assisté — un marqueur, un clic, rien d'autre

Date : 2026-10-14 · Statut : **abandonné, remplacé par ADR-049**. Première déclaration : `6292e2efcf75`.

- Contexte : Un ancien userscript pouvait choisir un relais ou cliquer sans intention explicite.
- Décision et raison : Un remplacement avait limité le clic natif à un marqueur de lien, consommé une seule fois.
- Conséquences : Abandonné intégralement par ADR-049 le 14 octobre 2026, y compris marqueur, harnais et documentation d’installation. Ce récit n’autorise aucune réintroduction.

<a id="adr-049"></a>

## ADR-049 : Aucun userscript, aucune extension, aucun script injecté — le seul parcours est intégré au bot

Date : 2026-10-14 · Statut : **accepté**. Première déclaration : `6292e2efcf75`.

- Contexte : L’utilisateur retire toute méthode userscript, extension ou injection de page.
- Décision et raison : Supprimer le parcours local assisté d’ADR-048 ; seul subsiste le callback serveur d’ADR-047, derrière son interrupteur et avec lien manuel de repli.
- Conséquences : Remplace ADR-048. La réussite authentifiée de bout en bout reste non démontrée à la base de cette tâche. Un blocage n’autorise ni contournement ni retour à une méthode abandonnée.
- Observation historique du 14 octobre, source de consolidation `6292e2e` : refus anonyme et défi DataDome classés, suspension observée ; ce ne sont pas une réussite authentifiée ni une observation actuelle. Détails dans [la procédure checkout](../vinted-checkout.md).
