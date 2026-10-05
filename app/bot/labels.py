"""Libellés et règles de présentation communes du bot Telegram.

Charte unique de présentation : titres d'écrans, libellés de champs, statuts en
toutes lettres, vocabulaire des données absentes. Tout gabarit de message passe
par ici : un libellé ne peut pas s'écrire « Prix max » d'un côté et « Prix
maximum » de l'autre.

Les statuts (`Active`, `En pause`) et les mentions d'absence (« Non renseigné »,
« Indisponible », « À confirmer ») sont des constantes : la présentation ne
réinvente pas la donnée, elle la nomme. Les enums restent dans `app.domain`.

Trois règles de mise en forme, valables sur **tous** les écrans :

1. **Un libellé de champ est gras, sa valeur reste en romain.**
   `**Prix maximum :** 15,00 €` — jamais `**Prix maximum : 15,00 €**`. La valeur
   est la donnée ; la mettre en gras avec le libellé noie la lecture au lieu de
   la guider.
2. **Un texte d'instruction, d'exemple ou d'aide est en italique.**
   « *(Tape /cancel à tout moment pour annuler)* », « *(ex: PS5)* », le pied de
   confirmation. L'italique sépare ce que le bot demande de ce que le bot
   affirme — le lecteur ne confond plus une consigne avec une donnée.
3. **Un avertissement est en italique et isolé par une ligne vide.**
   Ambiguïté d'ISBN, moteur IA muet : la note vit dans son propre bloc, jamais
   collée entre deux informations du livre. Deux avertissements ne se composent
   pas de deux styles : tous portent l'italique.
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
TITLE_HELP = "📖 Commandes"
TITLE_STATS = "📊 Statistiques"
TITLE_AUTOTUNE = "🧹 Auto-tuning des alertes"
TITLE_EDIT_EXCLUDES = "✏️ Mots-clés exclus"
TITLE_OFFER = "💬 Offre suggérée"
TITLE_AI_ALERT = "⚠️ Analyse IA de vigilance"

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
FIELD_PERIOD = "Période"
FIELD_PRICE = "Prix"
# « Total est. » et non « Total estimé » : le montant n'est pas un prix
# Vinted mais notre calcul (article + protection + port), et l'abréviation
# tient la ligne sans la tronquer sur un téléphone.
FIELD_ESTIMATED_TOTAL = "Total est."
FIELD_FALSE_POSITIVES = "Faux positifs analysés"
FIELD_SUGGESTED_EXCLUDES = "Mots-clés exclus suggérés"
FIELD_SUGGESTED_EXCLUDES_CUSTOM = "Mots-clés exclus suggérés (personnalisés)"

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
BTN_BACK_TO_AUTOTUNE = "← Retour à la liste"
BTN_APPLY_EXCLUSIONS = "Appliquer les exclusions"
BTN_EDIT_EXCLUSIONS = "Modifier la liste"
BTN_EDIT_EXCLUSIONS_AGAIN = "Modifier à nouveau"
BTN_DISCARD_CHANGES = "← Revenir sans modifier"
BTN_EDIT_ALERT_SETTINGS = "⚙️ Modifier l'alerte"
BTN_ANALYSIS_RUNNING = "⏳ Analyse en cours"
# Un bouton ne répète pas l'action qu'il propose déjà à l'écran : « Confirmer »
# suffit, le message juste au-dessus dit ce qui est confirmé.
BTN_CONFIRM = "Confirmer"

# Sélecteur de période : la période courante est marquée, l'autre reste neutre.
BTN_PERIOD_7 = "7 jours"
BTN_PERIOD_30 = "30 jours"


def period_button(label: str, is_current: bool) -> str:
    """Bouton de période, coché quand il correspond à la période affichée."""
    return f"✅ {label}" if is_current else label

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


def format_price_line(
    item_price_cents: int,
    estimated_total_cents: int | None = None,
) -> str:
    """Ligne de prix unique du bot : « **Prix :** 4,00 € (**Total est. :** 7,78 €) ».

    Règle de charte, valable pour toutes les alertes et tous les messages
    futurs : le prix de l'annonce d'abord, le total estimé entre parenthèses.
    Seuls les libellés sont gras (règle 1 de la charte) ; les montants restent
    en romain. Sans total connu, la ligne se réduit au prix — elle n'invente rien
    et ne laisse pas de parenthèse vide derrière elle.
    """
    price = format_price_cents(item_price_cents)
    if estimated_total_cents is None:
        return f"<b>{FIELD_PRICE} :</b> {price}"
    total = format_price_cents(estimated_total_cents)
    return f"<b>{FIELD_PRICE} :</b> {price} (<b>{FIELD_ESTIMATED_TOTAL} :</b> {total})"


def escape_html(value: str | None) -> str:
    """Échappe toute valeur dynamique : la donnée source ne modifie pas le formatage."""
    return html.escape(value or "", quote=False)


def join_list(values: list[str] | None) -> str:
    """Liste de valeurs échappées et jointes, ou « Non renseigné » si vide."""
    if not values:
        return NOT_PROVIDED
    return ", ".join(escape_html(value) for value in values)
