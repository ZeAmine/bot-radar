"""Libellés et règles de présentation communes du bot Telegram.

Charte unique de présentation : titres d'écrans, libellés de champs, statuts en
toutes lettres, vocabulaire des données absentes. Tout gabarit de message passe
par ici : un libellé ne peut pas s'écrire « Prix max » d'un côté et « Prix
maximum » de l'autre.

Les statuts (`Active`, `En pause`) et les mentions d'absence (« Non renseigné »,
« Indisponible », « À confirmer ») sont des constantes : la présentation ne
réinvente pas la donnée, elle la nomme. Les enums restent dans `app.domain`.

Six règles de présentation, valables sur **tous** les écrans :

1. **Un libellé de champ est gras, sa valeur reste en romain.**
   `**Prix maximum :** 15,00 €` — jamais `**Prix maximum : 15,00 €**`. La valeur
   est la donnée ; la mettre en gras avec le libellé noie la lecture au lieu de
   la guider.
2. **Un texte d'instruction, d'exemple ou d'aide est en italique.**
   « *(Tape /cancel à tout moment pour annuler)* », « *(ex: PS5)* », le pied de
   confirmation. L'italique sépare ce que le bot demande de ce que le bot
   affirme — le lecteur ne confond plus une consigne avec une donnée.
3. **Un avertissement est en italique et isolé par une ligne vide.**
   Ambiguïté d'ISBN, moteur IA muet, ouvrage que personne n'identifie : les
   avertissements vivent **ensemble**, dans un seul bloc en tête de carte, jamais
   collés entre deux informations du livre — et jamais dispersés en haut et en bas
   de l'écran, ce qui obligeait à faire l'aller-retour pour savoir ce qui manque.
   Deux avertissements ne se composent pas de deux styles : tous portent l'italique,
   et deux notes voisines ne se séparent pas d'une ligne vide.
   Une **indication** sur ce que l'alerte surveille — une série multi-tomes — suit
   la même forme : italique, isolée, et **hors de la liste des informations**,
   sous le titre de l'écran. Elle dit *pourquoi* l'identifiant est omis ; elle
   n'est pas une donnée de fiche et ne prend donc pas rang avec elles.
4. **Une flèche ne sert à rien dans un bouton.**
   `Retour`, jamais `← Retour` : la flèche ne dit rien que le verbe ne dise
   déjà, et le lecteur doit la décoder pour retrouver une action pourtant
   écrite en toutes lettres — sans que le bouton en fasse davantage. Elle se
   retire donc à la source, pas à chaque endroit qui l'emploie : ni la
   constante ni le littéral n'en portent, et `test_no_button_carries_an_arrow`
   tient les deux formes. La flèche d'un **titre** (`Détection → alerte`)
   décrit un flux, pas une action : elle n'entre pas dans cette règle.
5. **Les actions d’une annonce « Voir l’annonce », « Prix du marché »,
   « Faux positif » et « Négocier » restent sans émoji**, y compris lorsque leur
   clavier est reconstruit.
6. **Un texte affiché écrit le tiret normal, jamais le cadratin.**
   Messages, boutons, aides et confirmations utilisent « - ». Un titre ou une
   notice source garde sa forme stockée : `escape_display` normalise son rendu à
   la frontière d'affichage, sans toucher aux données ni aux règles de matching.
"""

import html

from app.domain.enums import ItemCondition
from app.domain.rules.cote_outcome import CoteOutcome, PrixBasOutcome
from app.utils.price import format_cents

# Mentions d'absence : une donnée absente n'est jamais un zéro.

NOT_PROVIDED = "Non renseigné"
UNAVAILABLE = "Indisponible"
TO_CONFIRM = "À confirmer"

# Statuts d'alerte, en toutes lettres, sans emoji.

STATUS_ACTIVE = "Active"
STATUS_PAUSED = "En pause"

# Indication courte du récap de création : les détails restent dans les lignes
# « Variantes » et « Mots-clés exclus ».
CRITERIA_ENRICHED_NOTE = "Critères enrichis."

# Titres d'écrans : un seul emoji de fonction, en tête de message.

TITLE_NEW_OFFER = "🆕 {title}"
TITLE_ALERTS = "📚 Mes alertes"
TITLE_CATEGORIES = "📁 Catégories"
TITLE_CREATE_ALERT = "➕ Créer une alerte"
TITLE_EDIT_ALERT = "✏️ Modifier l'alerte"
TITLE_ALERT_EXISTS = "⚠️ Alerte déjà existante"
TITLE_HELP = "📖 Commandes"
TITLE_STATS = "📊 Statistiques"
TITLE_AUTOTUNE = "🧹 Auto-tuning des alertes"
# Titre propre à l'écran de proposition, sans quoi il faut relire le corps du message.
TITLE_EXCLUSIONS_PROPOSAL = "🧹 Exclusions proposées"
TITLE_EDIT_EXCLUDES = "✏️ Mots-clés exclus"
TITLE_OFFER = "💬 Offre suggérée"

FIELD_NAME = "Nom"
FIELD_ISBN = "ISBN"
FIELD_MAX_PRICE = "Prix maximum"
FIELD_INCLUDE_KEYWORDS = "Mots-clés inclus"
FIELD_EXCLUDE_KEYWORDS = "Mots-clés exclus"
FIELD_VARIANTS = "Variantes"
FIELD_CONDITIONS = "États acceptés"
FIELD_STATUS = "Statut"
FIELD_TITLE = "Titre"
FIELD_AUTHOR = "Auteur"
FIELD_PUBLISHER = "Éditeur"
FIELD_EDITION = "Édition"
FIELD_PERIOD = "Période"
FIELD_PRICE = "Prix"
# Pas de libellé détaillé : « TTC » suffit et garde la ligne lisible sur téléphone.
TOTAL_SUFFIX_TTC = "TTC"
FIELD_FALSE_POSITIVES = "Faux positifs"
FIELD_SUGGESTED_EXCLUDES = "Mots-clés exclus suggérés"
FIELD_SUGGESTED_EXCLUDES_CUSTOM = "Mots-clés exclus suggérés (personnalisés)"

# Cote d'un tome à la création : trois libellés, jamais un champ vide à la place.
FIELD_COTE = "Cote"
FIELD_PARUTION = "Parution"
FIELD_LOW_1Y = "Prix bas constaté (1 an)"
# Fin de phrase d'un prix de cote : le total est toujours port compris.
COTE_SHIPPING_INCLUDED = "port inclus"

# États d'offre en toutes lettres pour une ligne de cote : les codes du référentiel
# (NAE, NSE, TBE) sont faits pour les boutons et les filtres, pas pour une phrase.
COTE_CONDITION_LABELS: dict[str, str] = {
    "NAE": "neuf avec étiquette",
    "NSE": "neuf sans étiquette",
    "TBE": "très bon état",
}


def cote_condition_label(code: str | None) -> str:
    """État d'une offre retenue, en toutes lettres ; le code brut si inconnu.

    Même règle qu'`abbreviate_condition`, en sens inverse : un état que le
    référentiel de la cote ne nomme pas est rendu tel quel plutôt que perdu.
    """
    if not code:
        return NOT_PROVIDED
    return COTE_CONDITION_LABELS.get(code, code)


def mid_sentence(label: str) -> str:
    """Un libellé de marchand dans une phrase : « Très bon état + » → « très bon état + ».

    Le libellé n'est pas traduit — c'est celui du marchand, et son « + » veut dire
    quelque chose ; seule sa majuscule d'initiale appartient au début de ligne.
    """
    stripped = label.strip()
    return stripped[:1].lower() + stripped[1:] if stripped else stripped


def cote_line(outcome: CoteOutcome) -> str:
    """Ligne de cote : l'offre retenue, ou l'état exact qui explique son absence.

    Les quatre états du module de décision ont chacun leur phrase : « Non
    renseigné » décrit une donnée absente de la fiche, jamais une source muette,
    une collecte vide ou un plafond qui a filtré — trois faits différents qui se
    réparaient autrement.
    """
    if outcome.state == "cote_trouvee" and outcome.offre is not None:
        offer = outcome.offre
        parts = [format_cents(offer.total_cents), escape_display(offer.marchand)]
        if offer.etat:
            # Un marchand qui ne qualifie pas l'exemplaire n'a pas d'état à citer :
            # écrire « Non renseigné » à sa place inventerait un champ manquant.
            parts.append(cote_condition_label(offer.etat))
        parts.append(COTE_SHIPPING_INCLUDED)
        return f"<b>{FIELD_COTE} :</b> {parts[0]} - {', '.join(parts[1:])}"
    if outcome.state == "aucune_offre":
        return _cote_absence("aucune offre trouvée", outcome)
    if outcome.state == "erreur_recuperation":
        return _cote_absence("récupération impossible, la source n'a pas répondu", outcome)
    cheapest = outcome.moins_chere_ecartee_cents
    detail = f", la moins chère à {format_cents(cheapest)}" if cheapest is not None else ""
    return (
        f"<b>{FIELD_COTE} :</b> offres écartées par les filtres "
        f"({outcome.offres_examinees} offres{detail})"
    )


def _cote_absence(explanation: str, outcome: CoteOutcome) -> str:
    """Une cote absente, et le prix déjà connu s'il y en a un — daté, pas promu."""
    retained = outcome.derniere_offre_cents
    if retained is None:
        return f"<b>{FIELD_COTE} :</b> {explanation}"
    return (
        f"<b>{FIELD_COTE} :</b> {explanation} "
        f"(dernière offre connue : {format_cents(retained)})"
    )


def low_line(outcome: PrixBasOutcome) -> str:
    """Ligne du plus bas relevé sur douze mois, état marchand conservé tel quel."""
    if outcome.state == "prix_bas_trouve" and outcome.cents is not None:
        return (
            f"<b>{FIELD_LOW_1Y} :</b> {format_cents(outcome.cents)} - "
            f"{escape_display(mid_sentence(outcome.etat or ''))}, {COTE_SHIPPING_INCLUDED}"
        )
    if outcome.state == "aucun_releve_12_mois":
        return f"<b>{FIELD_LOW_1Y} :</b> aucun relevé sur 12 mois"
    return f"<b>{FIELD_LOW_1Y} :</b> historique indisponible"

BTN_PREVIOUS = "Précédent"
BTN_NEXT = "Suivant"
BTN_BACK = "Retour"
BTN_VIEW_LISTING = "Voir l'annonce"
BTN_CLOSE = "Fermer"
BTN_CANCEL = "Annuler"
BTN_DELETE = "Supprimer"
BTN_EDIT = "Modifier"
BTN_PAUSE = "Mettre en pause"
BTN_RESUME = "Réactiver"
BTN_CREATE = "Créer l'alerte"
BTN_CATEGORIES = "Catégories"
BTN_ALERTS_LIST = "Liste des alertes"
BTN_BACK_TO_MENU = "Retour au menu"
BTN_BACK_TO_RECAP = "Retour au récapitulatif"
BTN_BACK_TO_AUTOTUNE = "Retour à la liste"
# Un libellé qui revient n'a qu'une source, sinon il finit écrit autrement ailleurs.
BTN_BACK_TO_ALERT = "Retour à l'alerte"
BTN_BACK_TO_FIELDS = "Retour aux champs"
BTN_APPLY_EXCLUSIONS = "Appliquer"
BTN_EDIT_EXCLUSIONS = "Modifier"
BTN_IGNORE_EXCLUSIONS = "Ignorer"
BTN_EDIT_EXCLUSIONS_AGAIN = "Modifier à nouveau"
BTN_DISCARD_CHANGES = "Revenir sans modifier"
BTN_EDIT_ALERT_SETTINGS = "⚙️ Modifier l'alerte"
BTN_ANALYSIS_RUNNING = "⏳ Analyse en cours"
# La note d'échec du récap nomme ce bouton : le libellé vit ici pour que note et
# clavier ne divergent pas.
BTN_AI_VARIANTS_CREATE = "Variantes & Exclusions IA"
# Le message au-dessus dit ce qui est confirmé : un bouton « Confirmer » suffit.
BTN_CONFIRM = "Confirmer"
BTN_REPORTED = "Signalé"

# Sélecteur de période : la période courante est marquée, l'autre reste neutre.
BTN_PERIOD_7 = "7 jours"
BTN_PERIOD_30 = "30 jours"


def period_button(label: str, is_current: bool) -> str:
    """Bouton de période, coché quand il correspond à la période affichée."""
    return f"✅ {label}" if is_current else label


def format_price_cents(cents: int | None) -> str:
    """Montant en centimes, ou « Non renseigné » — jamais zéro à la place d'un inconnu."""
    if cents is None:
        return NOT_PROVIDED
    return f"{cents / 100:.2f} €".replace(".", ",")


# Abréviations d'états valables partout, messages comme boutons : un libellé complet
# tiendrait une ligne à lui seul, trois lettres font tenir une série sur une ligne.
CONDITION_SHORT: dict[str, str] = {
    ItemCondition.NEW_WITH_TAG.value: "NAE",
    ItemCondition.NEW_WITHOUT_TAG.value: "NSE",
    ItemCondition.VERY_GOOD.value: "TBE",
    ItemCondition.GOOD.value: "BE",
    ItemCondition.SATISFACTORY.value: "SAT",
}


def abbreviate_condition(condition: str | None) -> str:
    """Un état en abrégé (« TBE »), son libellé tel quel s'il est hors référentiel.

    Une abréviation manquante ne doit jamais effacer l'état : une source peut
    nommer un état que le référentiel ignore, et « Non renseigné » serait alors
    un mensonge. Un état absent, lui, reste « Non renseigné ».
    """
    if not condition:
        return NOT_PROVIDED
    return CONDITION_SHORT.get(condition, condition)


def format_conditions(conditions: list[str] | None) -> str:
    """États acceptés en abrégé (« NAE, NSE, TBE »), « Non renseigné » si vide.

    Même règle qu'`abbreviate_condition` : un état hors référentiel est rendu tel
    quel plutôt que perdu.
    """
    if not conditions:
        return NOT_PROVIDED
    return ", ".join(CONDITION_SHORT.get(condition, condition) for condition in conditions)


def format_price_line(
    item_price_cents: int,
    estimated_total_cents: int | None = None,
) -> str:
    """Ligne de prix unique du bot : « **Prix :** 4,00 € (7,78 € TTC) ».

    Règle de charte, valable pour toutes les alertes et tous les messages
    futurs : le prix de l'annonce d'abord, le total entre parenthèses, marqué
    « TTC » et non d'un libellé.

    « TTC » reste en romain avec les montants : il qualifie le total, il n'est
    pas le libellé d'un champ (règle 1 — seuls « Prix : » est gras). Le montant
    reste affiché même si l'écart de marché se calcule sur lui : sans lui, un
    écart de +0,42 € sur un prix de 20,00 € ne se recompose plus à l'œil et passe
    pour une erreur de calcul.

    Sans total connu, la ligne se réduit au prix — elle n'invente rien et ne
    laisse pas de parenthèse vide derrière elle.
    """
    price = format_price_cents(item_price_cents)
    if estimated_total_cents is None:
        return f"<b>{FIELD_PRICE} :</b> {price}"
    total = format_price_cents(estimated_total_cents)
    return f"<b>{FIELD_PRICE} :</b> {price} ({total} {TOTAL_SUFFIX_TTC})"


def escape_html(value: str | None) -> str:
    """Échappe toute valeur dynamique : la donnée source ne modifie pas le formatage."""
    return html.escape(value or "", quote=False)


def escape_display(value: str | None) -> str:
    """Échappe un titre ou un nom source pour l'affichage, sans tiret cadratin.

    Un titre de catalogue, un nom d'alerte, un titre d'annonce ou un critère affiché
    garde sa forme stockée : seule sa lecture change, et le tiret cadratin y devient
    un tiret normal. Les écrans qui réaffichent une saisie pour la modifier passent
    par `escape_html` : ce que l'utilisateur relit pour le corriger reste exactement
    ce qu'il a écrit.
    """
    return html.escape((value or "").replace("—", "-"), quote=False)


# Au-delà, la liste devient illisible sur un téléphone ; le reste est compté, jamais
# passé sous silence : le nombre annoncé est celui de l'inventaire, pas de l'extrait.
MAX_LISTED_FALSE_POSITIVES = 3


def exclusion_proposal_message(
    alert_name: str,
    disliked_titles: list[str],
    exclusions: list[str],
) -> str:
    """Le message de proposition d'exclusions, écrit une seule fois.

    Le scan proactif et l'analyse demandée produisaient deux textes différents pour
    la même décision — l'un avec des emoji décoratifs et un paragraphe d'explication,
    l'autre sans. Un seul gabarit les réunit : ce que l'utilisateur lit ne dépend plus
    du chemin qui l'a mené là.

    La forme est courte par construction. Le titre dit la proposition, le nom dit
    l'alerte, les faux positifs **montrent** ce qui a été rejeté — sans quoi les
    exclusions proposées seraient à croire sur parole —, et les exclusions proposées
    sont la seule chose à décider. L'explication du moteur est retirée : elle
    reformulait ce que la liste des faux positifs montre déjà. Aucune question finale
    non plus — le bouton dit « Appliquer », il n'a pas besoin qu'on la lui pose.
    """
    listed = disliked_titles[:MAX_LISTED_FALSE_POSITIVES]
    bullets = "\n".join(f"• <i>{escape_html(title)}</i>" for title in listed)
    remainder = len(disliked_titles) - len(listed)
    if remainder > 0:
        bullets += f"\n• <i>… et {remainder} autre(s)</i>"

    return (
        f"{TITLE_EXCLUSIONS_PROPOSAL}\n\n"
        f"{FIELD_NAME} : « {escape_html(alert_name)} »\n\n"
        f"{FIELD_FALSE_POSITIVES} ({len(disliked_titles)}) :\n"
        f"{bullets}\n\n"
        f"{FIELD_SUGGESTED_EXCLUDES} :\n"
        f"<code>{escape_html(', '.join(exclusions))}</code>"
    )


def join_list(values: list[str] | None) -> str:
    """Liste de valeurs échappées et jointes, ou « Non renseigné » si vide."""
    if not values:
        return NOT_PROVIDED
    return ", ".join(escape_html(value) for value in values)
