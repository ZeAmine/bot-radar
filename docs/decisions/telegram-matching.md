# Décisions — telegram-matching

Statut : décisions datées ; vérifier le statut de chaque entrée.
Périmètre : contexte et raisons de ce thème, sans procédure d’exploitation implicite.
Contrat/code de référence : [lecture spécialisée](../contracts/telegram-ai.md).

<a id="adr-006"></a>

## ADR-006 : Suppression de la détection des défauts cachés ("Hidden Condition")

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Le catalogue public ne contient pas la description vendeur détaillée.
- Décision et raison : Retirer Hidden Condition : une requête par article et une analyse supplémentaire augmentaient coût et exposition pour une valeur insuffisante.
- Conséquences : Ne pas réintroduire cette lecture réseau comme enrichissement indispensable à l’envoi.

<a id="adr-007"></a>

## ADR-007 : Suppression du bouton "Payer" (`[ 💳 Payer ]`)

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Un lien préfabriqué de paiement échouait sans session et contexte de transaction.
- Décision et raison : Supprimer Payer et ses callbacks ; conserver l’accès direct à l’annonce.
- Conséquences : Le bouton Payer reste supprimé. ADR-047 (préparation de checkout au clic) a été retirée par ADR-051 ; ADR-049 exclut les userscripts, extensions et scripts injectés.

<a id="adr-008"></a>

## ADR-008 : Auto-Tuning conversationnel & Purge des faux positifs

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Les faux positifs déjà traités polluaient encore le hub et les suggestions étaient peu modifiables.
- Décision et raison : Autoriser l’édition manuelle d’une proposition, puis purger ses dislikes dans la transaction d’application.
- Conséquences : Annuler conserve les dislikes. La suggestion ne modifie pas seule les critères.

<a id="adr-015"></a>

## ADR-015 : Suppression de l'album d'images (l'alerte garde sa couverture)

Introduction Git : 2026-10-02 · Statut : **accepté**. Première déclaration : `1caa522600e6`.

- Contexte : Le catalogue ne rendait plus qu’une couverture ; obtenir un album exigeait une requête de plus.
- Décision et raison : Retirer galerie, préchargement et bouton d’album ; garder la couverture transportée par le catalogue.
- Conséquences : Pas de requête par annonce pour ressusciter une galerie indisponible.

<a id="adr-022"></a>

## ADR-022 : Libellé gras, consigne en italique, avertissement isolé

Introduction Git : 2026-10-05 · Statut : **accepté**. Première déclaration : `bd668b00c1d6`.

- Contexte : Consignes et valeurs perdaient leur hiérarchie dans les cartes Telegram.
- Décision et raison : Centraliser la charte dans labels : libellé gras, valeur romaine, consigne italique, avertissement isolé en italique.
- Conséquences : La docstring est consommée par test_charte_copy ; conserver son sens. ADR-029/031 précisent marqueurs et indication de série.

<a id="adr-024"></a>

## ADR-024 : Une variante qui énumère n'est pas une variante, une exclusion n'est pas un chiffre

Introduction Git : 2026-10-05 · Statut : **accepté**. Première déclaration : `504aa51a84a6`.

- Contexte : Des variantes énumérées et exclusions numériques élargissaient ou étouffaient les matchs.
- Décision et raison : Une règle pure d’hygiène traite tous les chemins d’écriture, avec déduplication, plafonds et motifs de retrait.
- Conséquences : ADR-027/030 précisent le titre ; les gardes d’identité actuelles contrôlent les nombres aussi en lecture et le mot reconnaissable seulement à l’écriture.

<a id="adr-027"></a>

## ADR-027 : Une exclusion se qualifie par le titre, une variante ne le répète pas

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `d8eb72af7eb0`.

- Contexte : Une même règle de titre pour variantes et exclusions supprimait des rejets utiles.
- Décision et raison : Une variante répétant le titre est redondante ; une exclusion qui tient dans le titre rejette le produit et doit être retirée.
- Conséquences : Un titre vide ne décide pas. ADR-030 étend le rejet aux exclusions qui ne font qu’énumérer le titre par numéro.

<a id="adr-029"></a>

## ADR-029 : Un libellé de trois lettres, un marqueur qui ferme la ligne

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `5138d614272b`.

- Contexte : Un marqueur d’écart en tête et un libellé long chargeaient la ligne de prix.
- Décision et raison : Rendre TTC avec les montants, le marqueur en fin de ligne et aucun marqueur approbateur pour un écart nul.
- Conséquences : Seul le libellé est gras. Ne changer ni total livré ni formule d’écart pour obtenir ce rendu.

<a id="adr-030"></a>

## ADR-030 : Une exclusion n'est jamais le titre plus un numéro

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `cd49a3b1d854`.

- Contexte : Titre suivi d’un numéro pouvait être accepté comme exclusion et bloquer les tomes recherchés.
- Décision et raison : Rejeter ces exclusions et qualifier le numéro sans détruire la graphie originale lors du découpage.
- Conséquences : Ne pas couper une chaîne originale avec des positions calculées sur une normalisation qui en change la longueur.

<a id="adr-031"></a>

## ADR-031 : L'indication de série sort de la liste des informations

Introduction Git : 2026-10-06 · Statut : **accepté**. Première déclaration : `40114603f848`.

- Contexte : La liste des notices de série se lisait comme une donnée de la fiche et doublonnait l’absence d’ISBN.
- Décision et raison : Afficher une indication de série unique hors de la liste des informations ; omettre la ligne ISBN correspondante.
- Conséquences : La règle conserve ses preuves de volumes ; le rendu n’en fait plus un inventaire. La carte complète est ensuite partagée par ADR-032.
