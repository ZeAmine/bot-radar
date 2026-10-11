"""Fonctions d'aide et de formatage mutualisées pour les flux d'alerte Telegram."""

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.formatter import alert_status_text, format_cents_to_euros
from app.bot.handlers._alerts_router import bind_creation_keyboard
from app.bot.keyboards.alerts import edition_ordinal, get_alert_confirmation_keyboard
from app.bot.labels import (
    FIELD_AUTHOR,
    FIELD_CONDITIONS,
    FIELD_EDITION,
    FIELD_EXCLUDE_KEYWORDS,
    FIELD_INCLUDE_KEYWORDS,
    FIELD_ISBN,
    FIELD_MAX_PRICE,
    FIELD_PUBLISHER,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_VARIANTS,
    NOT_PROVIDED,
    TITLE_ALERT_EXISTS,
    format_conditions,
    format_price_cents,
)
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    CATEGORY_EMOJIS,
    CATEGORY_LABELS,
    DEFAULT_BD_CONDITIONS,
    DEFAULT_CLOTHES_CONDITIONS,
    DEFAULT_CLOTHES_EXCLUSIONS,
    DEFAULT_GAMING_CONDITIONS,
    DEFAULT_GAMING_EXCLUSIONS,
    DEFAULT_TECH_CONDITIONS,
    DEFAULT_TECH_EXCLUSIONS,
    CategorySlug,
)
from app.db.models.alert import Alert
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.category_repo import CategoryRepository
from app.domain.enums import BookSearchStatus
from app.domain.rules.book_volume import requested_volume, volume_title
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from app.services.ai.schemas import book_context_from
from app.services.alert_service import AlertServiceError, parse_price_to_cents
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    AUTORITE_GOOGLE_BOOKS,
    AUTORITE_OPENLIBRARY,
    TITLE_SIMILARITY_MIN,
    format_edition_fragments,
    generate_recommended_keywords,
    parse_query_title_and_author,
    title_similarity,
)
from app.use_cases.book_search import (
    BookSearchResult,
    BookSearchUseCase,
    BookWorkChoice,
    InvalidIsbnError,
    metadata_from_result,
)
from app.utils.isbn import extract_clean_isbn

logger = logging.getLogger(__name__)

# Titre unique de la carte de récap. La carte est le rendu unique de
# `build_creation_recap` : tous les chemins qui y aboutissent doivent se reconnaître
# à la même fiche. Les en-têtes de parcours (« Alerte configurée en langage naturel »,
# « Variantes & exclusions IA », « Modification enregistrée ») avaient disparu parce
# qu'ils en multipliaient cinq pour un seul brouillon. Ce titre les remplace sans les
# réintroduire : il ne dépend ni du chemin ni du geste, donc deux rendus de la même
# fiche restent identiques.
RECAP_TITLE = "➕ <b>Créer une alerte</b>"

# Titre de la confirmation : la carte est la même, relue après l'enregistrement. Seul
# l'en-tête change — c'est ce que l'écran annonce —, et il est écrit ici pour qu'aucun
# des deux rendus n'en invente un second.
ALERT_CREATED_TITLE = "✅ <b>Alerte créée</b>"

# Pied du récap : une consigne, donc en italique (charte, règle 2). Il vit à côté du
# titre parce que `creation_card` le pose sur les deux écrans ; la confirmation a le
# sien, et les deux arrivent par le même paramètre, donc aucun ne peut l'oublier.
RECAP_FOOTER = "<i>Valide les critères ci-dessus avant de créer l'alerte.</i>"

# Indication d'une série : ce que l'alerte surveille, non une information du livre.
# C'est une phrase, pas une donnée de fiche : elle rend la disparition de l'identifiant
# intelligible (« l'ISBN est omis, et voici pourquoi »), donc elle vit **hors** de la
# liste des informations — sous le titre de la carte, en italique et isolée par une
# ligne vide (charte, règle 3). Une seule phrase, sans énumérer les tomes repérés : la
# liste des notices rendues par l'autorité n'est pas une information d'alerte, et un
# pavé de tomes ne se lit pas. Elle se dérive de `is_series`, jamais d'une donnée
# portée par le brouillon.
SERIES_INDICATION = "Série multi-tomes (veille sur tous les tomes)"

# Avertissement d'un ouvrage que personne n'a identifié : la carte ne peut alors
# montrer ni auteur, ni éditeur, ni ISBN, et le taire faisait passer une fiche vide
# pour une fiche oubliée. Il n'énumère aucune autorité (« ni la BNF ni OpenLibrary »)
# parce que cette liste change : c'est la troisième source (ADR-033) qui l'a montré.
UNIDENTIFIED_WORK_NOTE = (
    "⚠️ Ouvrage non identifié : aucune source ne catalogue ce titre. "
    "Vérifie l'orthographe, ou saisis l'ISBN."
)


# Pied de l'écran « prix maximum » : la carte montre ce qui est déjà posé — l'identité
# de l'œuvre et les critères de veille — et la seule chose qui manque est le montant.
# C'est une consigne, donc en italique (charte, règle 2), et elle est écrite une fois
# pour les deux chemins qui demandent ce prix.
PRICE_PROMPT_FOOTER = (
    "💶 <i>Indique ton prix maximum pour cette alerte (en €) (ou /cancel) :</i>"
)


def series_indication_block() -> list[str]:
    """L'indication de série telle qu'un écran la colle : en italique, puis sa ligne vide.

    Deux écrans la rendent — le récap de création et la confirmation d'alerte créée — et
    la charte (règle 3) demande la même forme des deux côtés. Le bloc est écrit une fois
    pour qu'aucun des deux ne l'isole à moitié ni ne l'oublie : c'est le même objet
    affiché deux fois, pas deux phrases qui se ressemblent.
    """
    return [f"<i>{SERIES_INDICATION}</i>", ""]


def _get_alert_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "AlertRepository", AlertRepository)(session)


def _get_category_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "CategoryRepository", CategoryRepository)(session)


def extract_inline_price_and_clean_query(query_str: str) -> tuple[str, int | None]:
    """Extrait un éventuel prix de fin de saisie sans confondre un numéro de modèle ou de tome.

    Exemples :
    - "deathbringer 50" -> ("deathbringer", 5000)
    - "Raspberry Pi 4" -> ("Raspberry Pi 4", None)
    - "Raspberry Pi 4 50€" -> ("Raspberry Pi 4", 5000)
    - "Raspberry Pi 4 50" -> ("Raspberry Pi 4", 5000)
    """
    clean_query = query_str.strip()
    parts = clean_query.split()
    if len(parts) <= 1:
        return clean_query, None

    last_token = parts[-1]
    has_currency = "€" in last_token or "eur" in last_token.lower()
    has_decimal = "." in last_token or "," in last_token

    # Pour un entier pur, n'interpréter comme prix que si >= 15 (exclut tome 1-10, Pi 4, PS 5...)
    is_plausible_price = False
    if has_currency or has_decimal:
        is_plausible_price = True
    else:
        cleaned_digits = last_token.replace(" ", "")
        if cleaned_digits.isdigit() and int(cleaned_digits) >= 15:
            is_plausible_price = True

    if is_plausible_price:
        try:
            price_cents = parse_price_to_cents(last_token)
            remaining_query = " ".join(parts[:-1]).strip()
            if remaining_query:
                return remaining_query, price_cents
        except AlertServiceError:
            pass

    return clean_query, None


def _join_escaped(values: list[str]) -> str:
    """Joint des fragments issus du moteur IA en échappant chacun.

    Mots-clés, variantes et états sont produits par le modèle : un `<b>` ou un `&`
    glissé dans l'un d'eux fait rejeter tout le récap par Telegram, qui répond
    « can't parse entities ». L'utilisateur reste alors bloqué sur
    « Recherche IA en cours » parce que Telegram rejoue l'update.
    """
    return ", ".join(html.escape(str(value)) for value in values)


def keyword_note(outcome: KeywordVariantsOutcome) -> str | None:
    """Ligne discrète expliquant ce que la recherche de mots-clés n'a pas trouvé.

    Cinq fins possibles, cinq libellés distincts : la carte ne doit jamais laisser
    croire qu'une liste vide est un avis du moteur alors que personne n'a été interrogé
    — ni qu'une exclusion manquante est un oubli quand elle est une décision.

    Le texte est rendu en italique par `creation_card` (charte, règle 3) : c'est un
    avertissement, jamais une information du livre. La mise en forme vit donc dans le
    rendu, pas ici : deux notes ne peuvent pas porter deux styles.

    Deux écrans l'affichent — le récap de `/add` et la reprise d'une alerte dont l'ISBN
    vient de changer — et aucun n'en a sa propre copie : le verdict d'une même fin ne
    doit pas dépendre du geste qui l'a déclenchée.
    """
    if not outcome.answered:
        # La relance a déjà eu lieu : ce n'est plus de la lenteur mais un échec. Le
        # bouton reste sous la carte, c'est donc lui qu'on nomme, pas « Modifier ».
        return (
            "⚠️ L'IA n'a pas répondu après une relance : aucune variante ni exclusion "
            "n'a été ajoutée. Appuie sur « ✨ Variantes & Exclusions IA » pour réessayer."
        )
    # ⚠️ et non 🔎 : ce n'est pas une observation en passant mais un manque, et la carte
    # le place en tête avec les autres avertissements. Le repérage d'un ouvrage
    # (« … identifié ») garde sa loupe : lui ne signale rien.
    if outcome.is_empty:
        return (
            "⚠️ L'IA n'a trouvé aucune variante ni exclusion pertinente : "
            "rien n'a été ajouté."
        )
    if not outcome.exclusions:
        return "⚠️ L'IA n'a trouvé aucune exclusion pertinente pour cet ouvrage."
    if not outcome.variants:
        return "⚠️ L'IA n'a trouvé aucune variante de frappe pour cet ouvrage."
    return None


def creation_card(
    data: dict[str, Any],
    *,
    header: str = RECAP_TITLE,
    footer: str = RECAP_FOOTER,
    extra_lines: list[str] | None = None,
) -> str:
    """La carte de création, construite **une fois** : récap et confirmation lisent celle-ci.

    `data` porte les clés du brouillon FSM — `name`, `author`, `publisher`, `isbn`,
    `isbn_note`, `is_series`, `category_slug`, `edition_alternatives`,
    `max_item_price_cents`, `selected_conditions`, `include_keywords`,
    `exclude_keywords`, `variants`, `keyword_note`, `book_unidentified`.

    Le récap et la confirmation divergeaient par trois cas — la ligne d'édition,
    l'identifiant introuvable et le bloc de cote — parce que deux blocs de code les
    écrivaient chacun de leur côté : une correction d'un côté laissait l'autre écran
    derrière. Ici, il n'y a plus qu'un texte. `header` et `footer` nomment l'écran et
    ce qu'il attend, `extra_lines` reçoit ce qui n'appartient qu'à la confirmation — la
    cote du livre créé, ou son attente. Une ligne ajoutée à un écran s'ajoute à
    l'autre ; elle ne peut plus y manquer.

    Ordre de la carte, du plus structurant au plus accessoire : l'indication de série,
    puis **toutes les notes réunies en un seul bloc** — hors de la liste des
    informations, en italique et isolées par une ligne vide (charte, règle 3) —
    l'identifiant, l'identité de l'œuvre (l'édition surveillée comprise, quand il y a un
    choix à faire), les critères de veille, et enfin le bloc d'issue avant le pied.

    L'identité et les critères se suivent **sans ligne vide** : la carte se lit
    d'un trait, et le blanc qui séparait l'auteur du prix ne servait qu'à faire
    croire à deux blocs.
    """
    # Le nom que l'alerte portera réellement : `save_alert` retombe sur « Alerte » quand
    # le brouillon n'en porte aucun, et la carte doit annoncer ce nom-là. Le défaut de
    # `dict.get` ne couvre que la clé absente — un nom vide ou nul passait au rendu tel
    # quel, et `html.escape` levait dessus.
    title = data.get("name") or "Alerte"
    author = data.get("author")
    publisher = data.get("publisher")
    isbn = data.get("isbn")
    isbn_note = data.get("isbn_note")
    is_series = data.get("is_series", False)
    edition_alternatives = data.get("edition_alternatives")
    include_keywords = data.get("include_keywords", [])
    exclude_keywords = data.get("exclude_keywords", [])
    variants = data.get("variants")
    keyword_note = data.get("keyword_note")
    # Aucune source n'a identifié cet ouvrage : la carte doit le dire **en tête**, et
    # montrer les trous de la fiche plutôt que de les laisser disparaître. Une donnée
    # absente est une information (ADR-028).
    book_unidentified = bool(data.get("book_unidentified"))
    # Une alerte hors livres — un objet, un jeu — n'a pas d'identifiant à trouver : sa
    # catégorie le dit, et elle vient du brouillon comme le reste.
    expects_isbn = (
        data.get("category_slug", CategorySlug.BD_COMICS.value)
        == CategorySlug.BD_COMICS.value
    )

    price_str = format_price_cents(data.get("max_item_price_cents"))
    cond_display = format_conditions(
        data.get("selected_conditions") or DEFAULT_BD_CONDITIONS
    )

    lines: list[str] = [header, ""]
    # L'indication de série ouvre la carte, isolée par une ligne vide : elle dit ce que
    # l'alerte surveille, et pourquoi elle n'a pas d'identifiant à montrer. Sa place
    # n'est pas dans la liste — elle ne décrit ni le titre, ni l'auteur, ni l'éditeur.
    if is_series:
        lines.extend(series_indication_block())
    # Toutes les notes forment **un seul bloc**, séparé de la liste par une ligne vide.
    # Elles étaient deux blocs — l'identité en haut, le moteur tout en bas — et l'esprit
    # devait faire l'aller-retour pour savoir ce qui manquait : côte à côte, les deux
    # avertissements se lisent d'un coup. L'ordre va du plus structurant au plus
    # accessoire : l'ouvrage non identifié d'abord, puis ce que l'autorité en dit, puis
    # le verdict du moteur sur les mots-clés.
    notes: list[str] = []
    if book_unidentified:
        notes.append(UNIDENTIFIED_WORK_NOTE)
    if isbn_note:
        # `isbn_note` porte des données de notice, donc échappé ; les deux autres sont
        # des libellés internes, rendus tels quels.
        notes.append(html.escape(isbn_note))
    if keyword_note:
        notes.append(keyword_note)
    if notes:
        lines.extend(f"<i>{note}</i>" for note in notes)
        lines.append("")

    # La ligne ISBN est toujours là, même vide. Son absence se confondait avec un
    # oubli, alors qu'elle porte l'information : « cet ouvrage n'a pas d'identifiant »
    # est justement ce qu'il faut lire avant de valider une alerte. Deux exceptions :
    # une alerte qui ne peut pas avoir d'identifiant — un objet, un jeu — n'a pas de
    # sujet à traiter, et une série a déjà dit, plus haut, que l'omission est un choix :
    # `non utilisé — série multi-tomes` répétait la même phrase deux fois.
    if not is_series:
        if isbn:
            lines.append(f"<b>{FIELD_ISBN} :</b> <code>{html.escape(isbn)}</code>")
        elif isbn_note:
            lines.append(f"<b>{FIELD_ISBN} :</b> À confirmer")
        elif expects_isbn:
            # Plus d'autorité nommée : la BNF n'est plus la seule à avoir été
            # interrogée. Le *pourquoi* et le *quoi faire* vivent dans l'avertissement
            # de tête, la ligne dit seulement que le champ est vide.
            lines.append(f"<b>{FIELD_ISBN} :</b> introuvable")

    volume_issue = creation_volume_issue(data)
    if volume_issue:
        lines.extend([f"<i>{html.escape(volume_issue)}</i>", ""])
    lines.append(f"<b>{FIELD_TITLE} :</b> {html.escape(title)}")
    # Un auteur ou un éditeur manquant s'affiche « Non renseigné » quand aucune source
    # n'a identifié l'ouvrage : la ligne disparaissait, et son absence se lisait comme
    # un oubli de la carte plutôt que comme une information sur l'ouvrage.
    if author:
        lines.append(f"<b>{FIELD_AUTHOR} :</b> {html.escape(author)}")
    elif book_unidentified or data.get("identity_unconfirmed"):
        lines.append(f"<b>{FIELD_AUTHOR} :</b> {NOT_PROVIDED}")
    if publisher:
        lines.append(f"<b>{FIELD_PUBLISHER} :</b> {html.escape(publisher)}")
    elif book_unidentified or data.get("identity_unconfirmed"):
        lines.append(f"<b>{FIELD_PUBLISHER} :</b> {NOT_PROVIDED}")
    if expects_isbn and not is_series:
        language = str(data.get("language") or "")
        label = {
            "en": "Anglais", "fr": "Français", "de": "Allemand", "es": "Espagnol",
            "it": "Italien", "pt": "Portugais", "ja": "Japonais",
        }.get(language)
        lines.append(f"<b>Langue :</b> {html.escape(label or language or 'Inconnue')}")
    # L'édition surveillée complète l'identité du livre : elle se lit juste après
    # l'éditeur, avec lequel l'utilisateur la recoupe, et avant que la carte ne passe
    # aux critères de veille. Elle n'apparaît que quand il y a un choix à faire.
    lines.extend(format_edition_line(edition_alternatives, isbn))

    lines.append(f"<b>{FIELD_MAX_PRICE} :</b> {price_str}")
    lines.append(f"<b>{FIELD_CONDITIONS} :</b> {html.escape(cond_display)}")
    lines.append(f"<b>{FIELD_INCLUDE_KEYWORDS} :</b> {_join_escaped(include_keywords)}")
    if variants:
        lines.append(f"<b>{FIELD_VARIANTS} :</b> {_join_escaped(variants)}")
    if exclude_keywords:
        lines.append(f"<b>{FIELD_EXCLUDE_KEYWORDS} :</b> {_join_escaped(exclude_keywords)}")

    # Ce qui n'appartient qu'à un écran arrive par `extra_lines` — le bloc de cote de
    # la confirmation — juste avant le pied : c'est ce que la carte vient de produire,
    # pas un critère de veille, donc cela ferme la liste des informations.
    if extra_lines:
        lines.extend(["", *extra_lines])

    # Le pied nomme ce que l'écran attend : le récap se relit avant de valider, la
    # confirmation annonce ce qui va suivre. Les deux arrivent par le même paramètre,
    # donc la carte ne peut pas en oublier un. C'est une consigne, donc en italique
    # (charte, règle 2).
    lines.extend(["", footer])
    return "\n".join(lines)


def build_edition_choices(resolution: Any) -> list[dict[str, Any]]:
    """Éditions d'une résolution BNF, converties en brouillon stockable dans le FSM."""
    if resolution is None:
        return []
    return [edition.model_dump() for edition in resolution.editions()]


# Formules d'édition qui ne distinguent rien par elles-mêmes : les retirer donne
# « Beauvi… » là où la notice écrit « les Éditions de Beauvi… ».
_PUBLISHER_NOISE = (
    "les éditions de ",
    "les éditions ",
    "éditions de ",
    "éditions ",
    "editions de ",
    "editions ",
    "édition de ",
    "édition ",
    "edition ",
    "les ",
    "l'",
    "de ",
    "d'",
)
# Un bouton Telegram tient ~30 caractères utiles ; au-delà il se fait tronquer
# n'importe où. On coupe nous-mêmes, sur un espace, jamais au milieu d'un mot.
_PUBLISHER_LABEL_MAX = 20


def _short_person(name: str | None) -> str:
    """« Nicolas Fébule » -> « Fébule » : un bouton n'a la place que d'un nom.

    Le prénom est le premier sacrifié : sur des homonymes, c'est le nom de famille
    qui distingue les auteurs, et deux libellés identiques retombent sur l'ISBN.
    """
    parts = [part for part in re.split(r"\s+", (name or "").strip()) if part]
    return parts[-1] if parts else ""


def _short_publisher(publisher: str | None) -> str:
    """« les Éditions de Beauvi… » -> « Beauvi… » : le nom propre, sans la formule."""
    text = (publisher or "").strip()
    lowered = text.casefold()
    for prefix in _PUBLISHER_NOISE:
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip()
            lowered = text.casefold()
    if len(text) > _PUBLISHER_LABEL_MAX:
        head = text[: _PUBLISHER_LABEL_MAX - 1]
        if " " in head:
            head = head.rsplit(" ", 1)[0]
        text = head.rstrip() + "…"
    return text


def _edition_button_text(edition: dict[str, Any]) -> str:
    """« Çà et là - 2022 » : l'éditeur d'abord, puis le millésime.

    L'éditeur prime parce qu'il distingue le plus souvent les éditions concurrentes
    d'une même œuvre (« poches » contre « album ») ; le millésime le suit quand la
    BNF le donne. Le libellé est réduit : un bouton se lit d'un coup d'œil.
    """
    publisher = _short_publisher(edition.get("publisher"))
    year = edition.get("publication_year")
    if publisher and year:
        return f"{publisher} - {year}"
    if publisher:
        return publisher
    return str(year) if year else ""


def _edition_identity(edition: dict[str, Any]) -> str:
    """« 2022 · Çà et là · 224 p. · 24 cm » : millésime, éditeur, puis format."""
    parts: list[str] = []
    year = edition.get("publication_year")
    if year:
        parts.append(str(year))
    publisher = str(edition.get("publisher") or "").strip()
    if publisher:
        parts.append(publisher)
    fragments = format_edition_fragments(edition.get("format_detail"))
    if fragments:
        parts.append(fragments)
    return " · ".join(parts)


def build_edition_labels(
    edition_alternatives: list[dict[str, Any]] | None,
) -> list[str]:
    """Libellés des éditions, alignés sur l'index du clavier (`al_create_edition:{index}`).

    Le numéro (1️⃣, 2️⃣…) est le seul marqueur visuel : il relie la ligne « Édition
    suivie » du récap au bouton qui bascule l'édition. Deux éditions que l'éditeur et
    le millésime ne départagent pas reçoivent l'ISBN : mieux vaut un libellé technique
    qu'un doublon indistinguable. Les libellés sont bruts (pas d'échappement HTML) : le
    texte du bouton est du plain, l'échappement se fait côté récap.
    """
    editions = list(edition_alternatives or [])
    identities = [_edition_button_text(edition) for edition in editions]
    labels: list[str] = []
    for index, edition in enumerate(editions):
        isbn13 = str(edition.get("isbn13") or "").strip()
        identity = identities[index]
        if not isbn13:
            labels.append("")
        elif not identity or identities.count(identity) > 1:
            labels.append(f"{edition_ordinal(index)} {isbn13}")
        else:
            labels.append(f"{edition_ordinal(index)} {identity}")
    return labels


def build_work_labels(work_choices: list[dict[str, Any]] | None) -> list[str]:
    """Libellés des œuvres homonymes, alignés sur l'index du clavier (`al_create_work:{index}`).

    « 2️⃣ Nef · 2025 · Éditions du Chêne » : l'auteur d'abord, parce que c'est lui qui
    distingue deux ouvrages de même titre, puis le millésime et l'éditeur. Sans auteur
    connu, l'ISBN prend sa place : deux boutons identiques ne seraient pas un choix.
    """
    works = list(work_choices or [])
    identities = [_work_button_text(work) for work in works]
    labels: list[str] = []
    for index, work in enumerate(works):
        isbn13 = str(work.get("isbn13") or "").strip()
        identity = identities[index]
        if not isbn13:
            labels.append("")
        elif not identity or identities.count(identity) > 1:
            labels.append(f"{edition_ordinal(index)} {isbn13}")
        else:
            labels.append(f"{edition_ordinal(index)} {identity}")
    return labels


def _work_button_text(work: dict[str, Any]) -> str:
    """« Nef · 2025 » : le nom de famille puis le millésime, chaque partie si connue.

    L'auteur prime : c'est lui qui distingue deux ouvrages de même titre. L'éditeur
    ne prend sa place qu'à défaut d'auteur — il ne sert alors qu'à ne pas laisser un
    bouton vide, et reste réduit comme celui d'une édition.
    """
    parts: list[str] = []
    author = _short_person(work.get("author"))
    if author:
        parts.append(author)
    year = work.get("publication_year")
    if year:
        parts.append(str(year))
    if not author:
        publisher = _short_publisher(work.get("publisher"))
        if publisher:
            parts.append(publisher)
    return " · ".join(parts)


def format_edition_line(
    edition_alternatives: list[dict[str, Any]] | None,
    current_isbn: str | None,
) -> list[str]:
    """Ligne « Édition » : l'édition retenue, décrite pour un humain.

    Affichée seulement quand plusieurs éditions sont réellement proposées : sans choix
    à faire, elle ne ferait que répéter l'identité déjà donnée plus haut dans la carte.
    Elle exige aussi un millésime ou un format : sans eux, elle se réduirait à
    l'éditeur, déjà porté par la ligne « Éditeur » et donc incapable de départager
    deux éditions.
    """
    editions = list(edition_alternatives or [])
    selectable = [e for e in editions if str(e.get("isbn13") or "").strip()]
    if not current_isbn or len(selectable) < 2:
        return []

    current_index = next(
        (
            index
            for index, edition in enumerate(editions)
            if str(edition.get("isbn13") or "").strip() == current_isbn
        ),
        None,
    )
    if current_index is None:
        return []

    edition = editions[current_index]
    if not edition.get("publication_year") and not format_edition_fragments(
        edition.get("format_detail")
    ):
        return []
    return [
        f"<b>{FIELD_EDITION} :</b> {html.escape(_edition_identity(edition))}"
    ]


def creation_volume_issue(data: dict[str, Any]) -> str | None:
    """Bloque aussi les anciens boutons si le volume n'a pas de preuve liée à l'ISBN."""
    if data.get("category_slug", CategorySlug.BD_COMICS.value) != CategorySlug.BD_COMICS.value:
        return None
    number = data.get("requested_volume") or requested_volume(str(data.get("name") or ""))
    if number is None:
        return None
    context = data.get("book_context") or {}
    if (
        not data.get("is_series")
        and data.get("isbn")
        and context.get("isbn13") == data["isbn"]
        and context.get("volume_number") == number
        and title_similarity(context.get("title"), data.get("name")) >= TITLE_SIMILARITY_MIN
    ):
        return None
    return (
        f"Le volume {number} demandé n’a pas pu être confirmé. "
        "Aucun autre volume ne sera ajouté à sa place."
    )


def creation_keyboard(data: dict[str, Any]) -> InlineKeyboardMarkup:
    """Clavier du récap : œuvres homonymes, éditions numérotées, puis validation.

    « ✨ Variantes & Exclusions IA » n'apparaît que tant que la recherche
    automatique n'a pas été menée. Une fois les mots-clés posés, le bouton ne
    pourrait rien ajouter que le brouillon ne contienne déjà, et il douterait de
    l'automatisme. L'état du brouillon décide donc, pas le chemin qui affiche.
    """
    alternatives = data.get("edition_alternatives")
    works = data.get("work_choices")
    keyboard = get_alert_confirmation_keyboard(
        show_variants_btn=not data.get("keywords_finalized"),
        edition_alternatives=alternatives,
        edition_labels=build_edition_labels(alternatives),
        work_choices=works,
        work_labels=build_work_labels(works),
    )

    if creation_volume_issue(data):
        keyboard.inline_keyboard = [
            [button for button in row if button.callback_data != "alert_create_confirm"]
            for row in keyboard.inline_keyboard
        ]
        keyboard.inline_keyboard = [row for row in keyboard.inline_keyboard if row]
    return bind_creation_keyboard(keyboard, data.get("creation_token"))


def build_creation_recap(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """`(texte, clavier)` du récap de création : une seule mise en forme, tout chemin.

    Cliquer une édition, corriger un prix, changer un mot-clé ou attendre la
    recherche de variantes ne change que les données de `data`. Le rendu reste
    identique : aucun de ces gestes ne peut produire une carte différente de celle
    qu'il vient de remplacer, ni laisser un en-tête de chemin concurrence. La
    confirmation de création lit la même carte, avec son propre en-tête et sa cote.
    """
    return creation_card(data), creation_keyboard(data)


def format_alerts_list_text(
    alerts: list[Alert],
    current_page: int,
    total_count: int,
    category_label: str | None = None,
) -> str:
    """Génère le texte de la vue compacte /list, dans l'ordre de la charte.

    Titre « Mes alertes », pagination et total sur une ligne distincte, une entrée
    par alerte : numéro, titre, statut, Prix maximum. L'ISBN et les filtres restent
    dans la fiche détaillée, chaque valeur dynamique est échappée.
    """
    total_pages = max(1, (total_count + 9) // 10)
    title_suffix = f" — {category_label}" if category_label else ""
    lines = [f"📚 <b>Mes alertes{title_suffix}</b>"]
    lines.append(
        f"Page {current_page + 1}/{total_pages} — "
        f"{total_count} alerte{'s' if total_count > 1 else ''}"
    )
    lines.append("")
    start_num = current_page * 10 + 1
    for i, a in enumerate(alerts, start=start_num):
        status_text = alert_status_text(a)
        cat_icon = (
            f"{a.category.emoji} "
            if getattr(a, "category", None) and getattr(a.category, "emoji", None)
            else ""
        )
        price_str = format_cents_to_euros(a.max_item_price_cents)
        lines.append(
            f"{i}. {cat_icon}<b>{html.escape(a.name)}</b> — {status_text} — {price_str}"
        )
    return "\n".join(lines)


async def _begin_bibliographic_search(state: FSMContext) -> int:
    """Réserve un jeton d'ordre pour une recherche bibliographique inline.

    Deux saisies rapprochées lancent deux recherches concurrentes : sans jeton, la
    plus lente écrirait son brouillon après la plus récente — l'écran montrerait
    l'œuvre de la première alors que l'utilisateur a déjà tapé la seconde. Le jeton
    est monotone, donc seul le dernier écrivain est légitime.
    """
    return await _bump_search_token(state)


async def _bump_search_token(state: FSMContext) -> int:
    """Invalide toute recherche en vol : le prochain écrivain sera le seul légitime.

    Appelé au départ d'une recherche, mais aussi par tout geste qui rebat les cartes
    du brouillon — changement de catégorie, saisie hors livres — pour qu'une réponse
    tardive ne s'écrive pas dans un écran qui ne l'attend plus.
    """
    current = await state.get_data()
    token = int(current.get("search_token") or 0) + 1
    await state.update_data(search_token=token)
    return token


async def _search_still_current(state: FSMContext, token: int) -> bool:
    """Le jeton est-il encore celui de cette recherche ?

    Un état vidé (annulation, création terminée) a perdu son jeton : la réponse
    tardive est alors périmée, comme si une autre saisie l'avait remplacée. C'est
    ce qui empêche un brouillon abandonné de ressusciter.
    """
    current = int((await state.get_data()).get("search_token") or 0)
    return current == token


def _work_choice_to_dict(choice: BookWorkChoice) -> dict[str, Any]:
    """Un choix d'œuvre du contrat en dictionnaire stockable dans le FSM."""
    return {
        "isbn13": choice.isbn13,
        "title": choice.title,
        "author": choice.author,
        "publisher": choice.publisher,
        "publication_year": choice.publication_year,
    }


def _edition_choice_to_dict(edition: Any) -> dict[str, Any]:
    """Une édition du contrat en dictionnaire stockable dans le FSM."""
    return {
        "isbn13": edition.isbn13,
        "publisher": edition.publisher,
        "publication_year": edition.publication_year,
        "format_detail": edition.format_detail,
        "volume_number": edition.volume_number,
        "language": edition.language,
    }


def draft_updates_from_search(
    result: BookSearchResult,
    *,
    query_title: str,
    category_id: str = "",
    category_slug: str = CategorySlug.BD_COMICS.value,
) -> dict[str, Any]:
    """Projette un `BookSearchResult` en clés brouillon, en une passe.

    La recherche initiale et le choix d'œuvre passent ici. Le réalignement d'un
    ISBN saisi conserve son exception explicite : le code volontairement saisi
    reste au brouillon même sans identité confirmée.

    Règles :
    - `found` seul attache un ISBN et remplit les champs confirmés ;
    - `ambiguous` propose des œuvres sans rien rattacher ;
    - `partial` affiche le candidat en lecture seule (work_choices) sans
      alimenter les champs confirmés, le BookContext ni la persistance ;
    - `not_found` / `unavailable` laissent l'identité vide, avec un avertissement
      distinct selon le motif réel.
    """
    number = requested_volume(query_title)
    query_title = volume_title(query_title, number) if number else query_title
    confirmed = result.confirmed
    is_found = result.status == BookSearchStatus.FOUND and confirmed is not None
    is_ambiguous = result.status == BookSearchStatus.AMBIGUOUS
    # Une série l'emporte sur l'identité : l'alerte surveille l'œuvre entière, pas le
    # tome que la notice a pu nommer. Elle porte donc le titre demandé, et pas d'ISBN.
    is_series = result.is_series

    if is_series:
        includes, _excludes, _variants = generate_recommended_keywords(query_title, None)
        return {
            "category_id": category_id,
            "requested_volume": number,
            "language": result.reference.language if result.reference else None,
            "category_slug": category_slug,
            "name": query_title,
            "isbn": None,
            "author": None,
            "publisher": None,
            "include_keywords": includes,
            "exclude_keywords": [],
            "variants": [],
            "selected_conditions": list(DEFAULT_BD_CONDITIONS),
            "isbn_note": None,
            "is_series": True,
            "work_choices": [],
            "edition_alternatives": [],
            "book_context": None,
            "book_unidentified": False,
            "identity_unconfirmed": False,
        }

    if is_found:
        assert confirmed is not None
        meta = metadata_from_result(result)
        assert meta is not None
        name = volume_title(confirmed.title, number) if number else confirmed.title
        includes, excludes, variants = generate_recommended_keywords(
            name, confirmed.author
        )
        return {
            "category_id": category_id,
            "requested_volume": number,
            "language": result.reference.language if result.reference else None,
            "category_slug": category_slug,
            "name": name,
            "isbn": result.reference.isbn13 if result.reference else None,
            "author": confirmed.author,
            "publisher": confirmed.publisher,
            "include_keywords": includes,
            "exclude_keywords": excludes,
            "variants": variants,
            "selected_conditions": list(DEFAULT_BD_CONDITIONS),
            "isbn_note": result.note,
            "is_series": False,
            "work_choices": [],
            "edition_alternatives": [
                _edition_choice_to_dict(edition) for edition in result.editions
            ],
            "book_context": book_context_from(meta).model_dump(),
            "book_unidentified": False,
            "identity_unconfirmed": False,
        }

    if is_ambiguous:
        name = query_title
        includes, _excludes, _variants = generate_recommended_keywords(name, None)
        return {
            "category_id": category_id,
            "requested_volume": number,
            "language": result.reference.language if result.reference else None,
            "category_slug": category_slug,
            "name": name,
            "isbn": None,
            "author": None,
            "publisher": None,
            "include_keywords": includes,
            "exclude_keywords": [],
            "variants": [],
            "selected_conditions": list(DEFAULT_BD_CONDITIONS),
            "isbn_note": result.note,
            "is_series": False,
            "work_choices": [_work_choice_to_dict(work) for work in result.works],
            "edition_alternatives": [],
            "book_context": None,
            "book_unidentified": False,
            "identity_unconfirmed": True,
        }

    # not_found / unavailable / partial : alerte textuelle, jamais d'ISBN
    name = query_title
    includes, excludes, variants = generate_recommended_keywords(name, None)
    if result.status == BookSearchStatus.PARTIAL:
        note = (
            "Livre repéré, mais aucune édition avec ISBN n'a pu être confirmée."
            if result.works and result.works[0].isbn13 is None
            else "Vérification bibliographique incomplète : ISBN non rattaché pour l'instant."
        )
    elif result.status == BookSearchStatus.UNAVAILABLE:
        note = "Vérification impossible pour l'instant. Tu peux créer l'alerte à partir du titre."
    else:
        note = None
    return {
        "category_id": category_id,
        "requested_volume": number,
        "language": result.reference.language if result.reference else None,
        "category_slug": category_slug,
        "name": name,
        "isbn": None,
        "author": None,
        "publisher": None,
        "include_keywords": includes,
        "exclude_keywords": excludes,
        "variants": variants,
        "selected_conditions": list(DEFAULT_BD_CONDITIONS),
        "isbn_note": note,
        "is_series": False,
        "work_choices": (
            [_work_choice_to_dict(work) for work in result.works]
            if result.status == BookSearchStatus.PARTIAL
            else []
        ),
        "edition_alternatives": [],
        "book_context": None,
        "book_unidentified": result.status == BookSearchStatus.NOT_FOUND,
        "identity_unconfirmed": True,
    }


async def _resolve_edition_choices(
    title: str, author_hint: str | None
) -> list[dict[str, Any]]:
    """Liste les éditions concurrentes de l'œuvre pour la saisie manuelle.

    Une seule recherche supplémentaire, par le cas d'usage partagé : elle ne fait
    qu'énumérer les éditions que le contrat expose. Un échec réseau ne doit jamais
    bloquer la création de l'alerte, il prive seulement l'utilisateur du choix.
    """
    try:
        result = await BookSearchUseCase().search_by_title(title, author_hint)
    except Exception as exc:
        logger.debug("Éditions concurrentes indisponibles pour « %s » : %s", title, exc)
        return []
    if result.status != BookSearchStatus.FOUND:
        # Un titre homonyme n'a pas d'éditions : celles du roman porteraient l'ISBN
        # d'un autre ouvrage dans le clavier de création. Une identité non confirmée
        # (repli ou notice sans ISBN) n'en propose pas davantage.
        return []
    return [_edition_choice_to_dict(edition) for edition in result.editions]


@dataclass(frozen=True)
class IsbnRealignment:
    """Effet d'une correction d'ISBN sur le brouillon en cours.

    `conflict` renseignée : les autorités rattachent cet ISBN à un autre ouvrage, la
    correction est refusée et `updates` est vide — le brouillon reste intact.
    """

    updates: dict[str, Any] = field(default_factory=dict)
    conflict: str | None = None


# Ce que l'ISBN précédent avait rapporté sur l'ouvrage, et que le nouveau contredit.
# Les variantes et les exclusions ont été raisonnées sur cette fiche-là : les garder
# ferait matcher l'alerte sur le mauvais livre, puisqu'une variante suffit à contourner
# la contrainte d'inclusion.
KEYWORDS_FROM_PREVIOUS_WORK: dict[str, Any] = {
    "variants": [],
    "exclude_keywords": [],
    "keyword_note": None,
    "keywords_finalized": False,
    "book_context": None,
    "work_choices": [],
    # Dès qu'un ISBN — même non confirmé — nomme l'ouvrage, ou qu'une œuvre homonyme
    # est désignée, le brouillon n'est plus celui d'un titre que personne ne connaît :
    # l'avertissement d'ouvrage non identifié n'a plus d'objet, et la note d'identité
    # qui l'accompagne porte la nuance de confirmation. Laisser le drapeau en place
    # ferait dire deux fois la même chose à la carte.
    "book_unidentified": False,
    "identity_unconfirmed": False,
}


def _isbn_verification_note(result: BookSearchResult | None) -> str:
    """Rend le bilan réel, sans transformer une panne de repli en panne BNF."""
    source_names = {
        AUTORITE_BNF: "BNF",
        AUTORITE_OPENLIBRARY: "OpenLibrary",
        AUTORITE_GOOGLE_BOOKS: "Google Books",
    }
    if result is None:
        return "ISBN saisi, non confirmé : vérification impossible, réessaie plus tard."
    if result.status == BookSearchStatus.FOUND:
        source = source_names.get(result.provenance or "")
        note = result.note or (f"Source {source}." if source else "Identité confirmée.")
    elif result.status == BookSearchStatus.PARTIAL:
        note = "ISBN saisi, non confirmé : candidat repéré, vérification incomplète."
        if result.works:
            candidate = result.works[0]
            author = f" de {candidate.author}" if candidate.author else ""
            source = source_names.get(candidate.provenance or "")
            provenance = f" ({source})" if source else ""
            note += f" Candidat : « {candidate.title} »{author}{provenance}."
    elif result.status == BookSearchStatus.UNAVAILABLE:
        note = "ISBN saisi, non confirmé : vérification incomplète, réessaie plus tard."
    else:
        note = "ISBN saisi, non confirmé : aucune notice exploitable dans les sources consultées."
    if result.consulted:
        sources = ", ".join(source_names.get(source, source) for source in result.consulted)
        note += f" Sources consultées : {sources}."
    if result.unavailable:
        sources = ", ".join(source_names.get(source, source) for source in result.unavailable)
        note += f" Sources indisponibles : {sources}."
    if result.status != BookSearchStatus.FOUND:
        note += " Tu peux surveiller le titre en retirant l’ISBN."
    return note


async def realign_draft_to_isbn(draft: dict[str, Any], isbn: str) -> IsbnRealignment:
    """Réécrit l'identité d'un brouillon à partir d'un ISBN saisi par l'utilisateur.

    Changer d'ISBN, c'est changer d'ouvrage : l'auteur, l'éditeur, la fiche IA et les
    éditions que l'ancien ISBN avait rapportés décrivent un autre livre. Les laisser en
    place produisait une carte qui se contredit — un ISBN de 2025 sous l'auteur d'un
    roman de 1989 — et qui ne se corrigeait plus ensuite.

    Quatre issues, toutes dites à l'utilisateur :
    - la BNF confirme cet ISBN pour ce titre : sa notice fait autorité ;
    - elle le rattache à un autre ouvrage : la correction est refusée, en nommant le
      vrai titre — une contradiction est une preuve, pas un silence ;
    - personne ne connaît ce code, trop récent pour être catalogué : l'ISBN saisi est
      gardé et l'identité précédente est vidée, car rien ne la rattache encore à cet ISBN ;
    - aucune autorité n'a répondu, alors qu'un repli avait répondu à leur place : même
      conservation, et le motif dit que la vérification est à reprendre plus tard.

    Elles sont disjointes par construction : `IsbnVerdict` ne permet pas d'en melanger deux,
    et une notice de repli n'y vaut jamais confirmation.
    """
    title = str(draft.get("name") or "").strip()
    number = draft.get("requested_volume") or requested_volume(title)
    if number:
        title = volume_title(title, number)

    # Consomme le contrat partagé, avec contrôle du titre : une panne BNF ne peut pas
    # faire passer un repli pour une confirmation (T9), et une contradiction reste un
    # refus motivé. Aucune décision d'autorité n'est réécrite ici.
    try:
        result: BookSearchResult | None = await BookSearchUseCase().search_by_isbn(
            isbn.strip(), expected_title=title or None
        )
    except InvalidIsbnError:
        return IsbnRealignment(
            conflict="ISBN invalide : 10 ou 13 chiffres avec une clé de contrôle correcte."
        )
    except Exception as exc:
        logger.warning("Vérification bibliographique impossible : %s", type(exc).__name__)
        result = None

    if result is not None and result.status == BookSearchStatus.FOUND:
        meta = metadata_from_result(result)
        assert meta is not None
        return IsbnRealignment(
            updates={
                # Le spread vient d'abord : il vide la fiche de l'ouvrage précédent, et
                # les clés écrites ensuite sont celles de la notice qui atteste l'ISBN.
                **KEYWORDS_FROM_PREVIOUS_WORK,
                "isbn": isbn,
                "is_series": False,
                "isbn_note": _isbn_verification_note(result),
                "author": meta.author,
                "publisher": meta.publisher,
                "language": meta.language,
                "name": volume_title(title, number) if number else title,
                "requested_volume": number,
                "edition_alternatives": await _resolve_edition_choices(title, meta.author),
                "book_context": book_context_from(meta).model_dump(),
            }
        )

    if (
        result is not None
        and result.status == BookSearchStatus.NOT_FOUND
        and result.conflict is not None
    ):
        who = f" de {result.conflict.author}" if result.conflict.author else ""
        return IsbnRealignment(
            conflict=(
                f"Cet ISBN ({isbn}) correspond à « {result.conflict.title} »{who}, "
                f"pas à « {title} »."
            )
        )

    if result is not None and result.status in (
        BookSearchStatus.UNAVAILABLE,
        BookSearchStatus.PARTIAL,
    ):
        # Le motif ne va pas au brouillon — il est technique ; l'utilisateur, lui, doit
        # savoir qu'une nouvelle tentative a du sens, et qu'aucune autorité n'a tranché.
        logger.warning(
            "Vérification bibliographique incomplète : %s", result.unavailable
        )

    if number:
        return IsbnRealignment(
            conflict=(
                f"Le volume {number} demandé n’a pas pu être confirmé. "
                "Aucun autre volume ne sera ajouté à sa place."
            )
        )
    return IsbnRealignment(
        updates={
            **KEYWORDS_FROM_PREVIOUS_WORK,
            "language": None,
            "isbn": isbn,
            "is_series": False,
            "isbn_note": _isbn_verification_note(result),
            "identity_unconfirmed": True,
            "author": None,
            "publisher": None,
            "edition_alternatives": [],
        }
    )


async def render_existing_alert_response(
    event: Message | CallbackQuery,
    state: FSMContext,
    existing: Alert,
    *,
    new_price_cents: int | None = None,
    target_msg: Any = None,
) -> None:
    """Affiche une alerte déjà surveillée et propose de mettre à jour son prix ou d'annuler.

    Arrêt net et binaire : aucune alerte concurrente n'est créée, aucune recherche de variantes
    n'est déclenchée, et le récapitulatif de confirmation de création n'est jamais affiché.
    """
    status_text = alert_status_text(existing)
    price_str = format_price_cents(existing.max_item_price_cents)

    category_slug = (
        existing.category.slug
        if getattr(existing, "category", None) and existing.category
        else None
    )
    if existing.isbn or category_slug == CategorySlug.BD_COMICS.value:
        cat_emoji = "📚"
        item_label = "Cette BD"
    else:
        cat_slug_enum = (
            CategorySlug(category_slug)
            if category_slug in {c.value for c in CategorySlug}
            else None
        )
        cat_emoji = CATEGORY_EMOJIS.get(cat_slug_enum, "🎯") if cat_slug_enum else "🎯"
        cat_text = CATEGORY_LABELS.get(cat_slug_enum, "Produit") if cat_slug_enum else "Produit"
        item_label = f"Ce produit ({cat_text})"

    isbn_line = (
        f"<b>{FIELD_ISBN} :</b> <code>{html.escape(existing.isbn)}</code>\n"
        if existing.isbn
        else ""
    )

    # Cas 1 : Un nouveau prix différent du prix actuel a été spécifié
    if new_price_cents is not None and new_price_cents != existing.max_item_price_cents:
        new_price_str = format_price_cents(new_price_cents)
        await state.update_data(
            existing_alert_id=str(existing.id),
            new_max_price_cents=new_price_cents,
        )
        agreement = "e" if item_label.startswith("Cette") else ""
        warn_text = (
            f"⚠️ <b>{TITLE_ALERT_EXISTS[2:].strip()}</b>\n\n"
            f"<i>{item_label} est déjà surveillé{agreement} dans tes alertes.</i>\n\n"
            f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n"
            f"{isbn_line}"
            f"<b>Prix maximum actuel :</b> {price_str}\n"
            f"<b>{FIELD_STATUS} :</b> {status_text}\n\n"
            f"<i>Tu as indiqué un prix de {new_price_str}.</i>\n"
            "<i>Souhaites-tu mettre à jour le prix maximum de cette alerte existante ?</i>"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Mettre à jour à {new_price_str}",
                        callback_data=f"upd_alert_price:{existing.id}:{new_price_cents}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Conserver l'existante",
                        callback_data="alert_create_cancel",
                    )
                ],
            ]
        )
    else:
        # Cas 2 : Pas de prix spécifié ou prix identique
        await state.update_data(existing_alert_id=str(existing.id))
        await state.set_state(AlertCreationState.waiting_for_max_price)
        agreement = "e" if item_label.startswith("Cette") else ""
        warn_text = (
            f"⚠️ <b>{TITLE_ALERT_EXISTS[2:].strip()}</b>\n\n"
            f"<i>{item_label} est déjà surveillé{agreement} dans tes alertes.</i>\n\n"
            f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n"
            f"{isbn_line}"
            f"<b>Prix maximum actuel :</b> {price_str}\n"
            f"<b>{FIELD_STATUS} :</b> {status_text}\n\n"
            "<i>Pour modifier son prix maximum, entre un nouveau montant "
            "ou choisis Conserver sans modification.</i>"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Conserver sans modification",
                        callback_data="alert_create_cancel",
                    )
                ]
            ]
        )

    edit_msg: Any = target_msg
    if edit_msg is None and isinstance(event, CallbackQuery) and event.message:
        edit_msg = event.message

    if edit_msg and hasattr(edit_msg, "edit_text"):
        await edit_msg.edit_text(warn_text, reply_markup=keyboard)
    elif isinstance(event, CallbackQuery):
        await event.answer()
        if event.message:
            await event.message.answer(warn_text, reply_markup=keyboard)
    else:
        await event.answer(warn_text, reply_markup=keyboard)


async def process_bd_query_lookup(
    event: Message | CallbackQuery,
    state: FSMContext,
    query_str: str,
    db_session: Any,
    db_user: User | None = None,
) -> None:
    """Recherche l'œuvre via BookMetadataService et propose la configuration automatique."""
    target_msg = event if isinstance(event, Message) else event.message
    if not target_msg:
        return

    # Vérifier si un prix inline était fourni en fin de saisie (ex: "titre 50" ou "978... 25")
    clean_query, inline_price_cents = extract_inline_price_and_clean_query(query_str)

    # GARDE ANTI-DOUBLON LOCAL IMMÉDIAT (zéro appel BNF si déjà surveillé en base)
    if db_user:
        alert_repo = _get_alert_repo(db_session)
        isbn_candidate = extract_clean_isbn(clean_query)
        existing = await alert_repo.find_existing(
            db_user.id, isbn=isbn_candidate, title=clean_query
        )
        if existing:
            await render_existing_alert_response(
                event,
                state,
                existing,
                new_price_cents=inline_price_cents,
            )
            return

    status_msg = await target_msg.answer("🔍 Recherche automatique de l'œuvre...")

    # Jeton d'ordre : une saisie plus récente invalide cette réponse si elle arrive après.
    token = await _begin_bibliographic_search(state)

    # Détection de la forme de la saisie (ISBN ou titre) — même règle que
    # l'ancien `resolve_input`, mais sans seconde logique de résolution.
    raw_digits = re.sub(r"[^0-9X]", "", clean_query.upper())
    is_isbn_form = len(raw_digits) in (10, 13) and (
        raw_digits.isdigit() or raw_digits.endswith("X")
    )

    try:
        if is_isbn_form:
            result = await BookSearchUseCase().search_by_isbn(clean_query)
        else:
            query_title, author_hint = parse_query_title_and_author(clean_query)
            result = await BookSearchUseCase().search_by_title(query_title, author_hint)
    except InvalidIsbnError:
        # ISBN mal formé : la carte « non identifiée » actuelle, sans appel réseau
        result = None

    if not await _search_still_current(state, token):
        # Une saisie plus récente a pris la main : ce brouillon n'a plus à s'écrire.
        logger.info("Recherche bibliographique « %s » périmée : écran inchangé.", clean_query)
        return

    if result is None or result.status == BookSearchStatus.NOT_FOUND:
        # Aucune source ne connaît ce titre — un livre sorti la semaine dernière n'est
        # dans aucun catalogue. Le cul-de-sac « aucune œuvre trouvée » obligeait à
        # deviner un titre que les autorités connaissent, c'est-à-dire à renoncer alors
        # que l'utilisateur, lui, sait ce qu'il cherche. La carte reste donc le chemin
        # unique : elle ouvre sur l'avertissement, montre les trous de la fiche, et
        # laisse créer l'alerte sur le titre saisi — l'ISBN reste saisissable par
        # « Modifier », et une correction ultérieure réaligne toute la fiche.
        query_title = parse_query_title_and_author(clean_query)[0]
        category_repo = _get_category_repo(db_session)
        category = await category_repo.get_by_slug(CategorySlug.BD_COMICS.value)
        updates = draft_updates_from_search(
            BookSearchResult(status=BookSearchStatus.NOT_FOUND),
            query_title=query_title,
            category_id=str(category.id) if category else "",
        )
        await state.update_data(**updates)

        if inline_price_cents is not None:
            await state.update_data(max_item_price_cents=inline_price_cents)
            await state.set_state(AlertCreationState.confirm_creation)
            recap_text, keyboard = build_creation_recap(await state.get_data())
            await status_msg.edit_text(recap_text, reply_markup=keyboard)
            return

        await state.set_state(AlertCreationState.waiting_for_max_price)
        await status_msg.edit_text(
            creation_card(await state.get_data(), footer=PRICE_PROMPT_FOOTER)
        )
        return

    # GARDE ANTI-DOUBLON APRÈS RÉSOLUTION. Une série n'a pas d'ISBN : c'est le
    # titre qui la dédouble, sinon deux volumes seraient deux alertes.
    if db_user and result.status != BookSearchStatus.AMBIGUOUS:
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(
            db_user.id,
            isbn=None if result.is_series else (
                result.reference.isbn13 if result.reference else None
            ),
            title=(
                parse_query_title_and_author(clean_query)[0]
                if result.is_series
                else (result.confirmed.title if result.confirmed else clean_query)
            ),
        )
        if existing:
            await render_existing_alert_response(
                event,
                state,
                existing,
                new_price_cents=inline_price_cents,
                target_msg=status_msg,
            )
            return

    # Catégorie BD & Comics par défaut
    category_repo = _get_category_repo(db_session)
    category = await category_repo.get_by_slug(CategorySlug.BD_COMICS.value)
    query_title = parse_query_title_and_author(clean_query)[0]
    updates = draft_updates_from_search(
        result,
        query_title=query_title,
        category_id=str(category.id) if category else "",
    )
    # Le BookContext n'est alimenté que par une identité confirmée
    if result.status == BookSearchStatus.FOUND and result.confirmed:
        meta = metadata_from_result(result)
        if meta is not None:
            updates["book_context"] = book_context_from(meta).model_dump()
    await state.update_data(**updates)

    if inline_price_cents is not None:
        await state.update_data(max_item_price_cents=inline_price_cents)
        await state.set_state(AlertCreationState.confirm_creation)
        draft = await state.get_data()
        recap_text, keyboard = build_creation_recap(draft)
        await status_msg.edit_text(recap_text, reply_markup=keyboard)
        return

    # Demander le prix max : la même carte que le récap, dont le montant est la seule
    # pièce manquante. L'écran ne construit plus ses lignes d'identité à la main — ni
    # le titre, ni l'auteur, ni l'éditeur, ni l'ISBN, ni les critères : il lit le
    # brouillon qu'il vient d'écrire, donc ce qu'il montre est ce qu'il créera. Les
    # libellés viennent de `labels`, et un état absent se dit « Non renseigné » plutôt
    # que « Non trouvé » : le prix n'est pas encore demandé, il n'est pas introuvable.
    await state.set_state(AlertCreationState.waiting_for_max_price)
    draft = await state.get_data()
    await status_msg.edit_text(creation_card(draft, footer=PRICE_PROMPT_FOOTER))


async def process_tech_or_clothes_query(
    event: Message | CallbackQuery,
    state: FSMContext,
    query_str: str,
    category_slug: str,
    db_session: Any,
    db_user: User | None = None,
) -> None:
    """Traite la recherche pour un produit high-tech ou vêtement sans passer par la BNF."""
    target_msg = event.message if isinstance(event, CallbackQuery) else event
    if not target_msg:
        return

    # Une saisie hors livres rebat les cartes du brouillon : toute recherche
    # bibliographique encore en vol devient périmée.
    await _bump_search_token(state)

    # Vérifier si un prix inline était fourni en fin de saisie (ex: "pi 4 50€" ou "titre 50")
    clean_query, inline_price_cents = extract_inline_price_and_clean_query(query_str)

    cat_slug_enum = CategorySlug(category_slug)

    # Garde anti-doublon : Vérifier si ce produit est déjà surveillé
    if db_user:
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(db_user.id, title=clean_query)
        if existing:
            await render_existing_alert_response(
                event,
                state,
                existing,
                new_price_cents=inline_price_cents,
            )
            return

    # Catégorie en base
    category_repo = _get_category_repo(db_session)
    category = await category_repo.get_by_slug(category_slug)
    cat_id_str = str(category.id) if category else ""

    # Découpage des mots-clés inclus (conserve les mots > 1 char ou les chiffres de versions)
    tokens = [w.strip() for w in clean_query.split() if len(w.strip()) > 1 or w.strip().isdigit()]
    includes = tokens if tokens else [clean_query]

    # Exclusions et conditions adaptées par catégorie
    if cat_slug_enum == CategorySlug.TECH:
        default_excludes = list(DEFAULT_TECH_EXCLUSIONS)
        default_conditions = list(DEFAULT_TECH_CONDITIONS)
    elif cat_slug_enum == CategorySlug.GAMING:
        default_excludes = list(DEFAULT_GAMING_EXCLUSIONS)
        default_conditions = list(DEFAULT_GAMING_CONDITIONS)
    else:
        default_excludes = list(DEFAULT_CLOTHES_EXCLUSIONS)
        default_conditions = list(DEFAULT_CLOTHES_CONDITIONS)

    await state.update_data(
        category_id=cat_id_str,
        category_slug=category_slug,
        name=clean_query,
        isbn=None,
        author=None,
        publisher=None,
        include_keywords=includes,
        exclude_keywords=default_excludes,
        variants=[],
        selected_conditions=default_conditions,
    )

    if inline_price_cents is not None:
        await state.update_data(max_item_price_cents=inline_price_cents)
        await state.set_state(AlertCreationState.confirm_creation)
        draft = await state.get_data()
        recap_text, keyboard = build_creation_recap(draft)
        if (
            isinstance(event, CallbackQuery)
            and event.message
            and hasattr(event.message, "edit_text")
        ):
            await event.message.edit_text(recap_text, reply_markup=keyboard)
        else:
            await target_msg.answer(recap_text, reply_markup=keyboard)
        return

    # Demander le prix max : la même carte que le récap, écrite à partir du brouillon
    # qu'on vient de poser. Le bandeau `🎯 … (Tech)` disparaît comme sur la confirmation
    # (ADR-032) : l'écran annonce la fiche, pas le chemin qui y mène.
    await state.set_state(AlertCreationState.waiting_for_max_price)
    prompt = creation_card(await state.get_data(), footer=PRICE_PROMPT_FOOTER)
    if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
        await event.message.edit_text(prompt)
    else:
        await target_msg.answer(prompt)
