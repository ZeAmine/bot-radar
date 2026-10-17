"""Fonctions d'aide et de formatage mutualisées pour les flux d'alerte Telegram."""

import html
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.creation_drafts import draft_guard
from app.bot.formatter import alert_status_text, format_cents_to_euros
from app.bot.handlers._alerts_router import bind_creation_keyboard
from app.bot.keyboards.alerts import edition_ordinal, get_alert_confirmation_keyboard
from app.bot.labels import (
    BTN_AI_VARIANTS_CREATE,
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
    CatalogueVerdict,
    authors_conflict,
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
    creation_bibliographic_identity,
    metadata_from_result,
    series_bibliographic_identity,
)
from app.utils.isbn import extract_clean_isbn

logger = logging.getLogger(__name__)

RECAP_TITLE = "➕ <b>Créer une alerte</b>"

ALERT_CREATED_TITLE = "✅ <b>Alerte créée</b>"

RECAP_FOOTER = "<i>Valide les critères ci-dessus avant de créer l'alerte.</i>"

SERIES_INDICATION = "Série multi-tomes (veille sur tous les tomes)"

UNIDENTIFIED_WORK_NOTE = (
    "⚠️ Ouvrage non identifié : aucune source ne catalogue ce titre. "
    "Vérifie l'orthographe, ou saisis l'ISBN."
)


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


async def parse_listing_id(callback: CallbackQuery) -> uuid.UUID | None:
    """UUID d'annonce porté par le callback, ou ``None`` — l'erreur est annoncée.

    Plusieurs boutons d'annonce lisent ce même identifiant ; la réponse « Annonce
    invalide » est la même partout, elle vit donc ici plutôt que recopiée de
    handler en handler.
    """
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""
    try:
        return uuid.UUID(raw_id)
    except (IndexError, ValueError):
        await callback.answer("Annonce invalide.", show_alert=True)
        return None


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
            remaining_query = " ".join(parts[:-1]).strip().rstrip(",; ")
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


def keyword_note(
    outcome: KeywordVariantsOutcome,
    *,
    retry_label: str | None = BTN_AI_VARIANTS_CREATE,
) -> str | None:
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

    `retry_label` nomme le bouton de relance de **l'écran où la note s'affiche**. La
    fiche d'une alerte existante n'en a aucun — son menu d'édition ne porte pas d'action
    IA — donc elle passe `None` : une note qui renvoie à un bouton absent est une
    consigne qui ne mène nulle part.
    """
    if not outcome.answered:
        head = (
            "⚠️ L'IA n'a pas répondu après une relance : aucune variante ni exclusion "
            "n'a été ajoutée."
        )
        if retry_label:
            return f"{head} Appuie sur « {retry_label} » pour réessayer."
        return f"{head} Réessaie plus tard."
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
    book_unidentified = bool(data.get("book_unidentified"))
    expects_isbn = (
        data.get("category_slug", CategorySlug.BD_COMICS.value)
        == CategorySlug.BD_COMICS.value
    )

    price_str = format_price_cents(data.get("max_item_price_cents"))
    cond_display = format_conditions(
        data.get("selected_conditions") or DEFAULT_BD_CONDITIONS
    )

    lines: list[str] = [header, ""]
    if is_series:
        lines.extend(series_indication_block())
    notes: list[str] = []
    if book_unidentified:
        notes.append(UNIDENTIFIED_WORK_NOTE)
    if isbn_note:
        notes.append(html.escape(isbn_note))
    if keyword_note:
        notes.append(keyword_note)
    if notes:
        lines.extend(f"<i>{note}</i>" for note in notes)
        lines.append("")

    if not is_series:
        if isbn:
            lines.append(f"<b>{FIELD_ISBN} :</b> <code>{html.escape(isbn)}</code>")
        elif isbn_note:
            lines.append(f"<b>{FIELD_ISBN} :</b> À confirmer")
        elif expects_isbn:
            lines.append(f"<b>{FIELD_ISBN} :</b> introuvable")

    volume_issue = creation_volume_issue(data)
    if volume_issue:
        lines.extend([f"<i>{html.escape(volume_issue)}</i>", ""])
    lines.append(f"<b>{FIELD_TITLE} :</b> {html.escape(title)}")
    if expects_isbn:
        language = str(data.get("language") or "")
        language_label = {"en": "Anglais", "fr": "Français", "de": "Allemand",
                          "es": "Espagnol", "it": "Italien", "pt": "Portugais",
                          "ja": "Japonais"}.get(language, language if language != "und" else "")
        sources = {"bnf": "BNF", "openlibrary": "OpenLibrary", "google_books": "Google Books"}
        provenance = ", ".join(sources.get(source, source)
                               for source in data.get("identification_sources") or [])
        for label, value in (
            ("Langue", language_label), (FIELD_AUTHOR, author), (FIELD_PUBLISHER, publisher),
            ("Année", data.get("publication_year")), ("Format", data.get("format_detail")),
            ("Source", provenance or sources.get(data.get("source") or "")),
        ):
            lines.append(f"<b>{label} :</b> {html.escape(str(value or NOT_PROVIDED))}")
    else:
        if author:
            lines.append(f"<b>{FIELD_AUTHOR} :</b> {html.escape(author)}")
        if publisher:
            lines.append(f"<b>{FIELD_PUBLISHER} :</b> {html.escape(publisher)}")
    lines.extend(format_edition_line(edition_alternatives, isbn))

    lines.append(f"<b>{FIELD_MAX_PRICE} :</b> {price_str}")
    lines.append(f"<b>{FIELD_CONDITIONS} :</b> {html.escape(cond_display)}")
    lines.append(f"<b>{FIELD_INCLUDE_KEYWORDS} :</b> {_join_escaped(include_keywords)}")
    if variants:
        lines.append(f"<b>{FIELD_VARIANTS} :</b> {_join_escaped(variants)}")
    if exclude_keywords:
        lines.append(f"<b>{FIELD_EXCLUDE_KEYWORDS} :</b> {_join_escaped(exclude_keywords)}")

    if is_series:
        lines.extend(["", *render_series_volumes(data)])
    if extra_lines:
        lines.extend(["", *extra_lines])

    lines.extend(["", footer])
    return "\n".join(lines)




def _get_creation_book_repo(session: Any) -> Any:
    """Dépôt du cache, sans interrogation des fournisseurs."""
    from app.db.repositories.book_repo import BookRepository
    return BookRepository(session)


async def populate_creation_prices(data: dict[str, Any], repo: Any) -> None:
    """Lit les caches des ISBN confirmés ; un tome sans ISBN reste dans la liste."""
    from app.services.market_price_service import MarketPriceService

    rows: list[dict[str, Any]] = data.get("series_volumes") or []
    for row in rows:
        book = await repo.get_by_isbn13(row["isbn13"]) if row.get("confirmed") else None
        summary = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
        row["price_lines"] = summary.split("\n") if summary else []



def refresh_creation_series(
    data: dict[str, Any], books: list[Any], catalogue: CatalogueVerdict | None = None,
) -> None:
    """Fusionne les deux découvertes confirmées, puis relit leurs propres caches."""
    from app.services.book_metadata_service import BookMetadata, SeriesVolume, select_series_volumes

    rows = data.get("series_volumes") or []
    rejected = catalogue.rejected_isbns if catalogue is not None else ()
    candidates = [SeriesVolume.from_stored(row) for row in rows
                  if row.get("isbn13") not in rejected]
    if catalogue is not None:
        candidates.extend(catalogue.volumes)
        data.update(series_total=catalogue.declared_volume_count,
                    series_inconsistent=catalogue.inconsistent_counts,
                    series_truncated=catalogue.truncated)
    known_numbers = {v.number for v in candidates}
    for book in books:
        meta = BookMetadata(title=book.title, language=None, source="")
        if meta.volume_number and meta.volume_number not in known_numbers:
            candidates.append(SeriesVolume(meta.volume_number, meta))
    selected = select_series_volumes(candidates, publisher=data.get("series_reference_publisher"))
    data["series_volumes"] = [v.stored() for v in selected]
    data.update(series_bibliographic_identity(selected))


def series_volume_label(rows: list[dict[str, Any]]) -> str:
    """Un vocabulaire unique, depuis l'édition française retenue, puis l'autorité."""
    for row in sorted(rows, key=lambda r: (
        r.get("language") != "fr", r.get("source") != "bnf", r.get("number", 0)
    )):
        marker = re.search(
            r"\b(livres?|tomes?|volumes?|vol\.?)\s*\d", str(row.get("title", "")), re.I,
        )
        if marker:
            word = marker.group(1).casefold()
            if word.startswith("livre"):
                return "Livre"
            return "Volume" if word.startswith("vol") else "Tome"
    return "Tome"


def render_series_volumes(data: dict[str, Any]) -> list[str]:
    """Rendu unique des tomes préparés ; aucun prix ni ISBN n'est inventé."""
    rows = data.get("series_volumes") or []
    label = series_volume_label(rows)
    lines: list[str] = []
    for row in rows:
        lines.append(f"📚 <b>{label} {row['number']}</b>")
        code = row.get("isbn13") if row.get("confirmed") else None
        isbn_line = f"<code>{html.escape(code)}</code>" if code else NOT_PROVIDED
        lines.append(f"<b>ISBN-13 :</b> {isbn_line}")
        source = {"bnf": "BNF", "openlibrary": "OpenLibrary (repli BNF)",
                  "google_books": "Google Books (repli BNF)"}.get(row.get("source"))
        lines.append(f"<b>Source :</b> {source or NOT_PROVIDED}")
        language = row.get("language")
        if language and language != "fr":
            lines.append(f"<b>Langue :</b> {html.escape(language)}")
        lines.extend(row.get("price_lines") or [
            "<i>⚠️ Cote indisponible</i>",
            "<b>Prix du marché :</b> Non renseigné",
            "<b>Prix bas constaté :</b> Non renseigné",
        ])
    if data.get("series_inconsistent"):
        lines.append("<i>Complétude non confirmée : les notices se contredisent sur le total.</i>")
    elif data.get("series_total") is None:
        lines.append("<i>Complétude non confirmée : le nombre total de tomes est inconnu.</i>")
    else:
        lines.append(f"<i>{data['series_total']} tomes annoncés par les notices.</i>")
    if data.get("series_truncated"):
        lines.append("<i>Limite de recherche atteinte : des notices peuvent manquer.</i>")
    return lines

def build_edition_choices(resolution: Any) -> list[dict[str, Any]]:
    """Éditions d'une résolution BNF, converties en brouillon stockable dans le FSM."""
    if resolution is None:
        return []
    return [edition.model_dump() for edition in resolution.editions()]


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
    par alerte : numéro, titre, prix maximum, puis statut entre parenthèses.
    L'ISBN et les filtres restent
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
        price_str = (
            (str(a.max_item_price_cents // 100) if a.max_item_price_cents % 100 == 0
             else f"{a.max_item_price_cents / 100:.2f}".replace(".", ",")) + "€"
            if a.max_item_price_cents is not None else format_cents_to_euros(None)
        )
        lines.append(
            f"{i}. {cat_icon}<b>{html.escape(a.name)}</b> : {price_str} ({status_text})"
        )
    return "\n".join(lines)


async def _begin_bibliographic_search(
    state: FSMContext, *, expected_creation_token: str | None = None,
    expected_search_token: int | None = None,
) -> int | None:
    """Réserve un jeton d'ordre pour une recherche bibliographique inline.

    Deux saisies rapprochées lancent deux recherches concurrentes : sans jeton, la
    plus lente écrirait son brouillon après la plus récente — l'écran montrerait
    l'œuvre de la première alors que l'utilisateur a déjà tapé la seconde. Le jeton
    est monotone, donc seul le dernier écrivain est légitime.
    """
    async with draft_guard(state):
        current = await state.get_data()
        if expected_creation_token is not None and (
            current.get("creation_token") != expected_creation_token
        ):
            return None
        if (expected_search_token is not None
                and current.get("search_token") != expected_search_token):
            return None
        return await _bump_search_token(state)


async def _bump_search_token(state: FSMContext) -> int:
    """Invalide toute recherche en vol : le prochain écrivain sera le seul légitime.

    Appelé au départ d'une recherche, mais aussi par tout geste qui rebat les cartes
    du brouillon — changement de catégorie, saisie hors livres — pour qu'une réponse
    tardive ne s'écrive pas dans un écran qui ne l'attend plus.
    """
    current = await state.get_data()
    token = max(int(current.get("search_token") or 0) + 1, uuid.uuid4().int)
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
    is_series = result.is_series

    if is_series:
        includes, _excludes, _variants = generate_recommended_keywords(query_title, None)
        return {
            "category_id": category_id,
            **creation_bibliographic_identity(result),
            "requested_volume": number,
            "category_slug": category_slug,
            "name": query_title,
            "isbn": None,
            "author": confirmed.author if confirmed else None,
            "include_keywords": includes,
            "exclude_keywords": [],
            "variants": [],
            "selected_conditions": list(DEFAULT_BD_CONDITIONS),
            "isbn_note": result.note,
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
            **creation_bibliographic_identity(result),
            "requested_volume": number,
            "language": result.reference.language if result.reference else None,
            "category_slug": category_slug,
            "name": name,
            "isbn": result.reference.isbn13 if result.reference else None,
            "author": confirmed.author,
            "publisher": confirmed.publisher,
            "publication_year": confirmed.publication_year,
            "format_detail": result.reference.format_detail if result.reference else None,
            "source": result.provenance,
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
            **creation_bibliographic_identity(result),
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
            **creation_bibliographic_identity(result),
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


KEYWORDS_FROM_PREVIOUS_WORK: dict[str, Any] = {
    "variants": [],
    "exclude_keywords": [],
    "keyword_note": None,
    "keywords_finalized": False,
    "book_context": None,
    "work_choices": [],
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


async def realign_draft_to_isbn(draft: dict[str, Any], isbn: str, *, replace_identity: bool = False,
) -> IsbnRealignment:
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

    try:
        result: BookSearchResult | None = await BookSearchUseCase().search_by_isbn(
            isbn.strip(), expected_title=(None if replace_identity or draft.get("book_unidentified")
                or draft.get("identity_unconfirmed") or title.startswith("Livre ISBN ")
                else title or None)
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
        if (not replace_identity and not draft.get("book_unidentified")
                and not draft.get("identity_unconfirmed")
                and (draft.get("isbn") or draft.get("book_context"))
                and authors_conflict(draft.get("author"), meta.author)):
            return IsbnRealignment(conflict=(
                f"Cet ISBN correspond à « {meta.title} » de {meta.author}, "
                f"pas à l’auteur de la fiche courante."
            ))
        if replace_identity:
            number = meta.volume_number or requested_volume(meta.title)
        canonical_title = volume_title(meta.title, number) if number else meta.title
        return IsbnRealignment(
            updates={
                **KEYWORDS_FROM_PREVIOUS_WORK,
                "isbn": isbn,
                "is_series": False,
                "isbn_note": _isbn_verification_note(result),
                "author": meta.author,
                "publisher": meta.publisher,
                "language": meta.language,
                "publication_year": meta.publication_year,
                "format_detail": meta.format_detail,
                "source": meta.source,
                "series_volumes": [],
                "series_total": None,
                "series_truncated": False,
                "series_inconsistent": False,
                "name": canonical_title,
                "include_keywords": generate_recommended_keywords(canonical_title)[0],
                "requested_volume": number,
                "edition_alternatives": await _resolve_edition_choices(
                    canonical_title, meta.author,
                ),
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
    expected_creation_token: str | None = None,
    expected_search_token: int | None = None,
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

    if new_price_cents is not None and new_price_cents != existing.max_item_price_cents:
        new_price_str = format_price_cents(new_price_cents)
        updates: dict[str, Any] = {"existing_alert_id": str(existing.id),
                   "new_max_price_cents": new_price_cents}
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
        updates = {"existing_alert_id": str(existing.id)}
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

    async with draft_guard(state):
        current = await state.get_data()
        if (expected_creation_token is not None
                and current.get("creation_token") != expected_creation_token):
            return
        if (expected_search_token is not None
                and current.get("search_token") != expected_search_token):
            return
        await state.update_data(**updates)
        if "new_max_price_cents" not in updates:
            await state.set_state(AlertCreationState.waiting_for_max_price)
    keyboard = bind_creation_keyboard(keyboard, current.get("creation_token"))
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


async def _apply_search_draft(
    state: FSMContext, token: int, updates: dict[str, Any], price: int | None,
) -> dict[str, Any] | None:
    async with draft_guard(state):
        if not await _search_still_current(state, token):
            return None
        if price is not None:
            updates["max_item_price_cents"] = price
        await state.update_data(**updates)
        await state.set_state(AlertCreationState.confirm_creation if price is not None
                              else AlertCreationState.waiting_for_max_price)
        return await state.get_data()


async def process_bd_query_lookup(
    event: Message | CallbackQuery,
    state: FSMContext,
    query_str: str,
    db_session: Any,
    db_user: User | None = None,
    expected_creation_token: str | None = None,
    expected_search_token: int | None = None,
) -> None:
    """Recherche l'œuvre via BookMetadataService et propose la configuration automatique."""
    target_msg = event if isinstance(event, Message) else event.message
    if not target_msg:
        return

    clean_query, inline_price_cents = extract_inline_price_and_clean_query(query_str)

    token = await _begin_bibliographic_search(
        state, expected_creation_token=expected_creation_token,
        expected_search_token=expected_search_token,
    )
    if token is None:
        return

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
                expected_search_token=token,
            )
            return

    status_msg = await target_msg.answer("🔍 Recherche automatique de l'œuvre...")

    raw_digits = re.sub(r"[^0-9X]", "", clean_query.upper())
    is_isbn_form = bool(extract_clean_isbn(clean_query)) or len(raw_digits) in (10, 13) and (
        raw_digits.isdigit() or raw_digits.endswith("X")
    )

    try:
        if is_isbn_form:
            result = await BookSearchUseCase().search_by_isbn(
                extract_clean_isbn(clean_query) or clean_query,
            )
        else:
            query_title, author_hint = parse_query_title_and_author(clean_query)
            result = await BookSearchUseCase().search_by_title(
                query_title, author_hint, include_series_catalogue=True,
            )
    except InvalidIsbnError:
        result = None

    if not await _search_still_current(state, token):
        logger.info("Recherche bibliographique « %s » périmée : écran inchangé.", clean_query)
        return

    if result is None or result.status == BookSearchStatus.NOT_FOUND:
        query_title = parse_query_title_and_author(clean_query)[0]
        category_repo = _get_category_repo(db_session)
        category = await category_repo.get_by_slug(CategorySlug.BD_COMICS.value)
        updates = draft_updates_from_search(
            BookSearchResult(status=BookSearchStatus.NOT_FOUND),
            query_title=query_title,
            category_id=str(category.id) if category else "",
        )
        updates.update(original_input=query_str, search_queries=[clean_query])
        draft = await _apply_search_draft(state, token, updates, inline_price_cents)
        if draft is None:
            return

        if inline_price_cents is not None:
            recap_text, keyboard = build_creation_recap(draft)
            await status_msg.edit_text(recap_text, reply_markup=keyboard)
            return

        await status_msg.edit_text(creation_card(draft, footer=PRICE_PROMPT_FOOTER))
        return

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
                expected_search_token=token,
                target_msg=status_msg,
            )
            return

    category_repo = _get_category_repo(db_session)
    category = await category_repo.get_by_slug(CategorySlug.BD_COMICS.value)
    query_title = parse_query_title_and_author(clean_query)[0]
    updates = draft_updates_from_search(
        result,
        query_title=query_title,
        category_id=str(category.id) if category else "",
    )
    await populate_creation_prices(updates, _get_creation_book_repo(db_session))
    if result.status == BookSearchStatus.FOUND and result.confirmed:
        meta = metadata_from_result(result)
        if meta is not None:
            updates["book_context"] = book_context_from(meta).model_dump()
    updates.update(original_input=query_str, search_queries=[clean_query])
    draft = await _apply_search_draft(state, token, updates, inline_price_cents)
    if draft is None:
        return

    if inline_price_cents is not None:
        recap_text, keyboard = build_creation_recap(draft)
        await status_msg.edit_text(recap_text, reply_markup=keyboard)
        return

    await status_msg.edit_text(creation_card(draft, footer=PRICE_PROMPT_FOOTER))


async def process_tech_or_clothes_query(
    event: Message | CallbackQuery,
    state: FSMContext,
    query_str: str,
    category_slug: str,
    db_session: Any,
    db_user: User | None = None,
    expected_creation_token: str | None = None,
    expected_search_token: int | None = None,
) -> None:
    """Traite la recherche pour un produit high-tech ou vêtement sans passer par la BNF."""
    target_msg = event.message if isinstance(event, CallbackQuery) else event
    if not target_msg:
        return

    token = await _begin_bibliographic_search(
        state, expected_creation_token=expected_creation_token,
        expected_search_token=expected_search_token,
    )
    if token is None:
        return

    clean_query, inline_price_cents = extract_inline_price_and_clean_query(query_str)

    cat_slug_enum = CategorySlug(category_slug)

    if db_user:
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(db_user.id, title=clean_query)
        if existing:
            await render_existing_alert_response(
                event,
                state,
                existing,
                new_price_cents=inline_price_cents,
                expected_search_token=token,
            )
            return

    category_repo = _get_category_repo(db_session)
    category = await category_repo.get_by_slug(category_slug)
    cat_id_str = str(category.id) if category else ""

    tokens = [w.strip() for w in clean_query.split() if len(w.strip()) > 1 or w.strip().isdigit()]
    includes = tokens if tokens else [clean_query]

    if cat_slug_enum == CategorySlug.TECH:
        default_excludes = list(DEFAULT_TECH_EXCLUSIONS)
        default_conditions = list(DEFAULT_TECH_CONDITIONS)
    elif cat_slug_enum == CategorySlug.GAMING:
        default_excludes = list(DEFAULT_GAMING_EXCLUSIONS)
        default_conditions = list(DEFAULT_GAMING_CONDITIONS)
    else:
        default_excludes = list(DEFAULT_CLOTHES_EXCLUSIONS)
        default_conditions = list(DEFAULT_CLOTHES_CONDITIONS)

    updates = {
        "category_id": cat_id_str,
        "category_slug": category_slug,
        "name": clean_query,
        "isbn": None,
        "author": None,
        "publisher": None,
        "include_keywords": includes,
        "exclude_keywords": default_excludes,
        "variants": [],
        "selected_conditions": default_conditions,
    }

    draft = await _apply_search_draft(state, token, updates, inline_price_cents)
    if draft is None:
        return
    if inline_price_cents is not None:
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

    prompt = creation_card(draft, footer=PRICE_PROMPT_FOOTER)
    if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
        await event.message.edit_text(prompt)
    else:
        await target_msg.answer(prompt)
