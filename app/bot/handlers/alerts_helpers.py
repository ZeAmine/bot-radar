"""Fonctions d'aide et de formatage mutualisées pour les flux d'alerte Telegram."""

import html
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.formatter import format_cents_to_euros
from app.bot.keyboards.alerts import edition_ordinal, get_alert_confirmation_keyboard
from app.bot.labels import (
    FIELD_AUTHOR,
    FIELD_CONDITIONS,
    FIELD_EXCLUDE_KEYWORDS,
    FIELD_INCLUDE_KEYWORDS,
    FIELD_ISBN,
    FIELD_MAX_PRICE,
    FIELD_PUBLISHER,
    FIELD_TITLE,
    FIELD_VARIANTS,
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
    AlertStatus,
    CategorySlug,
)
from app.db.models.alert import Alert
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.category_repo import CategoryRepository
from app.domain.rules.series_detection import SeriesSignal, detect_series
from app.services.ai.schemas import book_context_from
from app.services.alert_service import AlertServiceError, parse_price_to_cents
from app.services.book_metadata_service import (
    BookMetadataService,
    IsbnVerdict,
    ambiguity_note,
    format_edition_fragments,
    generate_recommended_keywords,
    parse_query_title_and_author,
    provenance_note,
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

# Ligne d'une série que le moteur a reconnue lui-même, sans numéro de tome à énumérer.
DEFAULT_SERIES_LINE = "Série multi-tomes — surveillance par tome"

# Au-delà, la ligne de récap deviendrait un pavé : on annonce le compte.
MAX_LISTED_VOLUMES = 6


def series_note_for(volumes: Sequence[str]) -> str:
    """Ligne de récap d'une série : les tomes repérés, et l'ISBN volontairement omis.

    Les deux chemins d'ajout — saisie manuelle et `/add` en langage naturel —
    l'utilisent, donc ils disent la même chose. La liste est bornée : un pavé de vingt
    tomes ne se lit pas, et le compte suffit à décider.
    """
    if not volumes:
        return DEFAULT_SERIES_LINE
    if len(volumes) > MAX_LISTED_VOLUMES:
        listed = ", ".join(volumes[:MAX_LISTED_VOLUMES])
        return (
            f"Série multi-tomes détectée — {len(volumes)} tomes repérés ({listed}, …) "
            "— veille sur tous les tomes, sans ISBN."
        )
    return (
        f"Série multi-tomes détectée ({', '.join(volumes)}) "
        "— veille sur tous les tomes, sans ISBN."
    )


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


def format_semi_auto_recap(
    title: str,
    author: str | None,
    publisher: str | None,
    isbn: str | None,
    max_price_cents: int | None,
    include_keywords: list[str],
    exclude_keywords: list[str],
    conditions: list[str] | None = None,
    variants: list[str] | None = None,
    is_series: bool = False,
    series_note: str | None = None,
    isbn_note: str | None = None,
    edition_alternatives: list[dict[str, Any]] | None = None,
    keyword_note: str | None = None,
) -> str:
    """Carte compacte du brouillon, telle qu'affichée avant « Créer l'alerte ».

    La carte s'ouvre sur `RECAP_TITLE`, puis une ligne par information, du plus
    structurant au plus accessoire : identifiant, identité de l'œuvre, critères de
    veille, puis l'édition surveillée quand il y a un choix à faire. Les états sont
    abrégés (`format_conditions`) pour que la carte tienne à l'écran sans
    devenir un pavé.

    Les deux notes — `isbn_note` (provenance d'ISBN douteuse, repli de source) et
    `keyword_note` (ce que la recherche de mots-clés a ou n'a pas trouvé) — sont des
    avertissements : en italique, dans leur propre bloc isolé par une ligne vide
    (charte, règle 3). `isbn_note` passe **au-dessus** de l'ISBN, dont il conditionne
    la confiance ; collé entre l'identifiant et le titre, il ne se lisait pas.
    Le pied de confirmation, lui, est une consigne, donc en italique (règle 2).
    """
    price_str = format_price_cents(max_price_cents)
    cond_display = format_conditions(
        conditions if conditions else DEFAULT_BD_CONDITIONS
    )

    lines: list[str] = [RECAP_TITLE, ""]
    # L'avertissement d'identité ouvre la carte, isolé par une ligne vide : il
    # conditionne la confiance dans l'ISBN qui suit, et n'est donc jamais coincé
    # entre deux informations du livre.
    if isbn_note:
        lines.append(f"<i>{html.escape(isbn_note)}</i>")
        lines.append("")

    if isbn:
        lines.append(f"<b>{FIELD_ISBN} :</b> <code>{html.escape(isbn)}</code>")
    elif isbn_note:
        lines.append(f"<b>{FIELD_ISBN} :</b> À confirmer")

    lines.append(f"<b>{FIELD_TITLE} :</b> {html.escape(title)}")
    if is_series:
        lines.append(series_note or DEFAULT_SERIES_LINE)
    if author:
        lines.append(f"<b>{FIELD_AUTHOR} :</b> {html.escape(author)}")
    if publisher:
        lines.append(f"<b>{FIELD_PUBLISHER} :</b> {html.escape(publisher)}")

    lines.append("")
    lines.append(f"<b>{FIELD_MAX_PRICE} :</b> {price_str}")
    lines.append(f"<b>{FIELD_CONDITIONS} :</b> {html.escape(cond_display)}")
    lines.append(f"<b>{FIELD_INCLUDE_KEYWORDS} :</b> {_join_escaped(include_keywords)}")
    if variants:
        lines.append(f"<b>{FIELD_VARIANTS} :</b> {_join_escaped(variants)}")
    if exclude_keywords:
        lines.append(f"<b>{FIELD_EXCLUDE_KEYWORDS} :</b> {_join_escaped(exclude_keywords)}")

    # Le verdict du moteur sur les mots-clés est un avertissement : italique et
    # séparé des informations du livre par une ligne vide. Les mots-clés et leurs
    # exclusions restent au-dessus, groupés ; seule la note qui les commente est
    # mise à part.
    if keyword_note:
        lines.extend(["", f"<i>{keyword_note}</i>"])

    edition_lines = format_edition_line(edition_alternatives, isbn)
    if edition_lines:
        lines.extend(["", *edition_lines])

    # Le pied de confirmation est porté par la carte elle-même : tous les chemins qui
    # aboutissent ici finissent sur le même clavier, donc une seule formulation, et
    # aucune ne peut la laisser de côté. L'alerte n'est pas encore créée : le texte
    # ne dit pas le contraire. C'est une consigne, donc en italique (charte).
    lines.extend(["", "<i>Valide les critères ci-dessus avant de créer l'alerte.</i>"])
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
    """Ligne « Édition suivie » : l'édition retenue, décrite pour un humain.

    Affichée seulement quand plusieurs éditions sont réellement proposées : sans choix
    à faire, elle ne ferait que répéter l'identité déjà donnée plus haut dans la carte.
    Elle exige aussi un millésime ou un format : sans eux, elle se réduirait à
    l'éditeur, déjà porté par la ligne « 🏢 Éditeur » et donc incapable de départager
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
        f"📖 <b>Édition suivie :</b> {html.escape(_edition_identity(edition))}"
    ]


def recap_from_state(data: dict[str, Any]) -> str:
    """Construit le récap de création à partir du brouillon FSM (rendu unique)."""
    return format_semi_auto_recap(
        title=data.get("name", "Alerte"),
        author=data.get("author"),
        publisher=data.get("publisher"),
        isbn=data.get("isbn"),
        max_price_cents=data.get("max_item_price_cents"),
        include_keywords=data.get("include_keywords", []),
        exclude_keywords=data.get("exclude_keywords", []),
        conditions=data.get("selected_conditions"),
        variants=data.get("variants"),
        is_series=data.get("is_series", False),
        series_note=data.get("series_note"),
        isbn_note=data.get("isbn_note"),
        edition_alternatives=data.get("edition_alternatives"),
        # La note accompagne le brouillon plutôt que l'appel : changer d'édition ou
        # modifier un prix re-rend la carte, et l'explication doit survivre au rendu.
        keyword_note=data.get("keyword_note"),
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
    return get_alert_confirmation_keyboard(
        show_variants_btn=not data.get("keywords_finalized"),
        edition_alternatives=alternatives,
        edition_labels=build_edition_labels(alternatives),
        work_choices=works,
        work_labels=build_work_labels(works),
    )


def build_creation_recap(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """`(texte, clavier)` du récap de création : une seule mise en forme, tout chemin.

    Cliquer une édition, corriger un prix, changer un mot-clé ou attendre la
    recherche de variantes ne change que les données de `data`. Le rendu reste
    identique : aucun de ces gestes ne peut produire une carte différente de celle
    qu'il vient de remplacer, ni laisser un en-tête de chemin concurrence.
    """
    return recap_from_state(data), creation_keyboard(data)


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
        status_text = "Active" if a.status == AlertStatus.ACTIVE else "En pause"
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


async def _resolve_edition_choices(
    title: str, author_hint: str | None
) -> list[dict[str, Any]]:
    """Liste les éditions concurrentes de l'œuvre pour la saisie manuelle.

    Une seule requête BNF supplémentaire : elle ne fait qu'énumérer ce que
    `resolve_for_alert` a déjà tranché. Un échec réseau ne doit jamais bloquer la
    création de l'alerte, il prive seulement l'utilisateur du choix.
    """
    try:
        resolution = await BookMetadataService().resolve_for_alert(title, author_hint)
    except Exception as exc:
        logger.debug("Éditions concurrentes indisponibles pour « %s » : %s", title, exc)
        return []
    if resolution is None or resolution.is_ambiguous:
        # Un titre homonyme n'a pas d'éditions : celles du roman porteraient l'ISBN
        # d'un autre ouvrage dans le clavier de création.
        return []
    return build_edition_choices(resolution)


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
}


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
    service = BookMetadataService()

    verdict = IsbnVerdict(outcome="inconnu")
    try:
        verdict = await service.confirm_isbn_for_title(isbn, title or None)
    except Exception as exc:
        logger.warning("Vérification BNF impossible pour l'ISBN %s : %s", isbn, exc)

    # Une seule issue à la fois : la notice n'est une réponse d'autorité que si le
    # verdict le dit, et la garde rend cette lecture impossible à rater.
    notice = verdict.notice if verdict.a_tranche else None

    if verdict.outcome == "contradiction" and notice is not None:
        who = f" de {notice.author}" if notice.author else ""
        return IsbnRealignment(
            conflict=(
                f"Cet ISBN ({isbn}) correspond à « {notice.title} »{who}, pas à « {title} »."
            )
        )

    if verdict.confirme and notice is not None:
        meta = notice
        return IsbnRealignment(
            updates={
                # Le spread vient d'abord : il vide la fiche de l'ouvrage précédent, et
                # les clés écrites ensuite sont celles de la notice qui atteste l'ISBN.
                **KEYWORDS_FROM_PREVIOUS_WORK,
                "isbn": isbn,
                "is_series": False,
                "isbn_note": provenance_note(meta),
                "author": meta.author,
                "publisher": meta.publisher,
                "edition_alternatives": await _resolve_edition_choices(title, meta.author),
                "book_context": book_context_from(meta).model_dump(),
            }
        )

    note = "ISBN saisi, non confirmé par la BNF"
    if verdict.panne:
        # Le motif ne va pas au brouillon — il est technique ; l'utilisateur, lui, doit
        # savoir qu'une nouvelle tentative a du sens, et qu'aucune autorité n'a tranché.
        logger.warning("Vérification BNF impossible pour l'ISBN %s : %s", isbn, verdict.panne)
        note = "ISBN saisi, non confirmé : la BNF n'a pas pu être interrogée, réessaie plus tard"

    return IsbnRealignment(
        updates={
            **KEYWORDS_FROM_PREVIOUS_WORK,
            "isbn": isbn,
            "is_series": False,
            "isbn_note": note,
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
    status_text = "Active" if existing.status == AlertStatus.ACTIVE else "En pause"
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
        f"<b>ISBN :</b> <code>{html.escape(existing.isbn)}</code>\n"
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
            f"{item_label} est déjà surveillé{agreement} dans tes alertes.\n\n"
            f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n"
            f"{isbn_line}"
            f"<b>Prix maximum actuel :</b> {price_str}\n"
            f"<b>Statut :</b> {status_text}\n\n"
            f"Tu as indiqué un prix de {new_price_str}.\n"
            "Souhaites-tu mettre à jour le prix maximum de cette alerte existante ?"
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
            f"{item_label} est déjà surveillé{agreement} dans tes alertes.\n\n"
            f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n"
            f"{isbn_line}"
            f"<b>Prix maximum actuel :</b> {price_str}\n"
            f"<b>Statut :</b> {status_text}\n\n"
            "Pour modifier son prix maximum, entre un nouveau montant "
            "ou choisis Conserver sans modification."
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

    service = BookMetadataService()
    resolution = await service.resolve_input(clean_query)

    if resolution is None:
        await status_msg.edit_text(
            f"❌ Aucune œuvre trouvée pour « <b>{html.escape(clean_query)}</b> ».\n\n"
            "<i>Tu peux réessayer avec un autre titre ou nom d'auteur.</i>"
        )
        await state.set_state(AlertCreationState.waiting_for_query)
        return

    meta = resolution.best
    # Un titre homonyme ne nomme aucun ouvrage : on n'en retient aucun, et les œuvres
    # trouvées sont proposées au choix. Même règle que le `/add` en langage naturel.
    ambiguous = resolution.is_ambiguous
    note = ambiguity_note(resolution) if ambiguous else None
    # Une notice qui porte un numéro de tome alors que la saisie n'en demande aucun
    # décrit un volume, pas l'œuvre : c'est la preuve d'une série. Rien de neuf n'est
    # interrogé — tout est déjà dans la résolution qu'on tient en main.
    query_title = parse_query_title_and_author(clean_query)[0]
    series = (
        SeriesSignal()
        if ambiguous
        else detect_series(
            query_title=query_title,
            notices=[meta.title, *(alt.title for alt in resolution.alternatives)],
        )
    )

    # GARDE ANTI-DOUBLON APRÈS RÉSOLUTION BNF. Une série n'a pas d'ISBN : c'est le
    # titre qui la dédouble, sinon deux volumes seraient deux alertes.
    if db_user and not ambiguous:
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(
            db_user.id,
            isbn=None if series.is_series else meta.isbn13,
            title=query_title if series.is_series else meta.title,
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
    cat_id_str = str(category.id) if category else ""

    # Une alerte de série porte le titre demandé, pas celui d'un volume : l'inclusion
    # dérivée de « … vol 2 » ne verrait jamais le tome 3. Les mots-clés sont donc
    # recalculés sur le titre de la série, en CPU, sans requête supplémentaire.
    if series.is_series:
        name = query_title
        includes, excludes, variants = generate_recommended_keywords(query_title, meta.author)
    else:
        name = meta.title
        includes = meta.recommended_includes
        excludes = [] if ambiguous else meta.recommended_excludes
        variants = [] if ambiguous else meta.recommended_variants

    await state.update_data(
        category_id=cat_id_str,
        category_slug=CategorySlug.BD_COMICS.value,
        name=name,
        isbn=None if (ambiguous or series.is_series) else meta.isbn13,
        author=None if ambiguous else meta.author,
        publisher=None if ambiguous else meta.publisher,
        include_keywords=includes,
        exclude_keywords=excludes,
        variants=variants,
        selected_conditions=list(DEFAULT_BD_CONDITIONS),
        isbn_note=note,
        is_series=series.is_series,
        series_note=series_note_for(series.volumes) if series.is_series else None,
        work_choices=(
            [work.model_dump() for work in resolution.work_choices()] if ambiguous else []
        ),
        # Les éditions concurrentes sont celles de la résolution qu'on tient déjà en
        # main : une seconde requête BNF n'ajouterait rien, et les homonymes d'un
        # titre ambigu ne seraient pas des éditions de l'œuvre demandée. Une série
        # n'en a pas non plus : chaque « édition » y serait celle d'un tome.
        edition_alternatives=(
            [] if (ambiguous or series.is_series) else build_edition_choices(resolution)
        ),
    )

    if inline_price_cents is not None:
        await state.update_data(max_item_price_cents=inline_price_cents)
        await state.set_state(AlertCreationState.confirm_creation)
        draft = await state.get_data()
        recap_text, keyboard = build_creation_recap(draft)
        await status_msg.edit_text(recap_text, reply_markup=keyboard)
        return

    # Demander le prix max
    await state.set_state(AlertCreationState.waiting_for_max_price)
    identity_lines = [f"📚 <b>{html.escape(name)}</b>"]
    if series.is_series:
        # Une série n'affiche pas d'ISBN : c'est le choix, pas un oubli.
        identity_lines.append(f"📖 {html.escape(series_note_for(series.volumes))}")
    identity_lines.append(f"✍️ <b>Auteur :</b> {html.escape(meta.author or 'Non spécifié')}")
    if not series.is_series:
        identity_lines.append(
            f"🏢 <b>Éditeur :</b> {html.escape(meta.publisher or 'Non spécifié')}"
        )
        identity_lines.append(f"🔢 <b>ISBN :</b> <code>{meta.isbn13 or 'Non trouvé'}</code>")
    prompt = (
        "\n".join(identity_lines)
        + "\n\n"
        + f"🔑 <b>Inclus :</b> {', '.join(includes)}\n"
        + f"🚫 <b>Exclus :</b> {', '.join(excludes)}\n"
        + "💎 <b>États :</b> Neuf avec étiquette, Neuf sans étiquette, Très bon état\n\n"
        + "💶 <i>Indique ton prix maximum pour cette alerte (en €) (ou /cancel) :</i>"
    )
    await status_msg.edit_text(prompt)


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

    # Vérifier si un prix inline était fourni en fin de saisie (ex: "pi 4 50€" ou "titre 50")
    clean_query, inline_price_cents = extract_inline_price_and_clean_query(query_str)

    cat_slug_enum = CategorySlug(category_slug)
    cat_emoji = CATEGORY_EMOJIS.get(cat_slug_enum, "🎯")
    cat_label = CATEGORY_LABELS.get(cat_slug_enum, "Produit")

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

    # Demander le prix max
    await state.set_state(AlertCreationState.waiting_for_max_price)
    prompt = (
        f"{cat_emoji} <b>{html.escape(clean_query)}</b> ({cat_label})\n\n"
        f"🔑 <b>Inclus :</b> {', '.join(includes)}\n"
        f"🚫 <b>Exclus :</b> {', '.join(default_excludes)}\n"
        f"💎 <b>États :</b> {', '.join(default_conditions)}\n\n"
        "💶 <i>Indique ton prix maximum pour cette alerte (en €) (ou /cancel) :</i>"
    )
    if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
        await event.message.edit_text(prompt)
    else:
        await target_msg.answer(prompt)
