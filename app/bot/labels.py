"""Libellés et règles de présentation communes du bot Telegram.

Charte unique de présentation : titres d'écrans, libellés de champs, statuts en
toutes lettres, vocabulaire des données absentes. Tout gabarit de message passe
par ici : un libellé ne peut pas s'écrire « Prix max » d'un côté et « Prix
maximum » de l'autre.

Les statuts (`Active`, `En pause`) et les mentions d'absence (« Non renseigné »,
« Indisponible », « À confirmer ») sont des constantes : la présentation ne
réinvente pas la donnée, elle la nomme. Les enums restent dans `app.domain`.
"""

import html

from app.domain.enums import ItemCondition

# ---------------------------------------------------------------------------
# Mentions d'absence — une donnée absente n'est jamais un zéro
# ---------------------------------------------------------------------------

NOT_PROVIDED = "Non renseigné"
UNAVAILABLE = "Indisponible"
TO_CONFIRM = "À confirmer"

# ---------------------------------------------------------------------------
# Statuts d'alerte, en toutes lettres, sans emoji
# ---------------------------------------------------------------------------

STATUS_ACTIVE = "Active"
STATUS_PAUSED = "En pause"

# ---------------------------------------------------------------------------
# Titres d'écrans — un seul emoji de fonction, en tête de message
# ---------------------------------------------------------------------------

TITLE_NEW_OFFER = "🆕 {title}"
TITLE_ALERTS = "📚 Mes alertes"
TITLE_CATEGORIES = "📁 Catégories"
TITLE_CREATE_ALERT = "➕ Créer une alerte"
TITLE_EDIT_ALERT = "✏️ Modifier l'alerte"
TITLE_ALERT_EXISTS = "⚠️ Alerte déjà existante"

# ---------------------------------------------------------------------------
# Libellés de champs — les mêmes mots d'un écran à l'autre
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Navigation et boutons — libellés explicites et constants
# ---------------------------------------------------------------------------

BTN_PREVIOUS = "← Précédent"
BTN_NEXT = "Suivant →"
BTN_BACK = "← Retour"
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
BTN_BACK_TO_MENU = "← Retour au menu"
BTN_BACK_TO_RECAP = "← Retour au récapitulatif"

# ---------------------------------------------------------------------------
# Helpers de formatage
# ---------------------------------------------------------------------------


def format_price_cents(cents: int | None) -> str:
    """Montant en centimes, ou « Non renseigné » — jamais zéro à la place d'un inconnu."""
    if cents is None:
        return NOT_PROVIDED
    return f"{cents / 100:.2f} €".replace(".", ",")


# Abréviations d'états : convention d'affichage du bot, valable partout — messages
# comme boutons. Un libellé complet tient une ligne à lui seul ; à trois lettres,
# une série d'états tient sur une seule ligne.
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


def escape_html(value: str | None) -> str:
    """Échappe toute valeur dynamique : la donnée source ne modifie pas le formatage."""
    return html.escape(value or "", quote=False)


def join_list(values: list[str] | None) -> str:
    """Liste de valeurs échappées et jointes, ou « Non renseigné » si vide."""
    if not values:
        return NOT_PROVIDED
    return ", ".join(escape_html(value) for value in values)
