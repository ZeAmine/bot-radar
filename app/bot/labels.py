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


def format_conditions_full(conditions: list[str] | None) -> str:
    """Libellés complets des états acceptés (« Neuf avec étiquette, Très bon état »).

    Un libellé hors référentiel est rendu tel quel : une correspondance manquante
    ne doit jamais effacer un critère d'acceptation de l'alerte.
    """
    if not conditions:
        return NOT_PROVIDED
    known = {condition.value for condition in ItemCondition}
    return ", ".join(conditions if not known else conditions)


def escape_html(value: str | None) -> str:
    """Échappe toute valeur dynamique : la donnée source ne modifie pas le formatage."""
    return html.escape(value or "", quote=False)


def join_list(values: list[str] | None) -> str:
    """Liste de valeurs échappées et jointes, ou « Non renseigné » si vide."""
    if not values:
        return NOT_PROVIDED
    return ", ".join(escape_html(value) for value in values)
