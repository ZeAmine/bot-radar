"""Fonctions d'aide et de formatage mutualisées pour les flux d'alerte Telegram."""

import html
import logging
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.formatter import format_cents_to_euros, format_conditions_short
from app.bot.keyboards.alerts import edition_ordinal, get_alert_confirmation_keyboard
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
from app.services.alert_service import AlertServiceError, parse_price_to_cents
from app.services.book_metadata_service import (
    BookMetadataService,
    format_edition_fragments,
)

logger = logging.getLogger(__name__)


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
    isbn_note: str | None = None,
    edition_alternatives: list[dict[str, Any]] | None = None,
) -> str:
    """Carte compacte du brouillon, telle qu'affichée avant « Créer l'alerte ».

    Une ligne par information, du plus structurant au plus accessoire : identifiant,
    identité de l'œuvre, critères de veille, puis l'édition surveillée quand il y a un
    choix à faire. Les états sont abrégés (`format_conditions_short`) pour que la carte
    tienne à l'écran sans devenir un pavé.

    `isbn_note` signale une provenance d'ISBN douteuse ou un repli de source : il reste
    affiché juste sous l'ISBN, dont il conditionne la confiance.
    """
    price_str = f"{max_price_cents / 100:.2f} €".replace(".", ",") if max_price_cents else "Aucun"
    cond_display = format_conditions_short(
        conditions if conditions else DEFAULT_BD_CONDITIONS
    )

    lines: list[str] = []
    if isbn:
        lines.append(f"🔢 <b>ISBN :</b> <code>{html.escape(isbn)}</code>")
    elif isbn_note:
        lines.append("🔢 <b>ISBN :</b> <i>non confirmé par la BNF</i>")
    if isbn_note:
        lines.append(f"{'ℹ️' if isbn else '⚠️'} <i>{html.escape(isbn_note)}</i>")

    lines.append(f"📌 <b>Titre :</b> {html.escape(title)}")
    if is_series:
        lines.append("📚 <b>Série multi-tomes</b> — surveillance par tome")
    if author:
        lines.append(f"✍️ <b>Auteur :</b> {html.escape(author)}")
    if publisher:
        lines.append(f"🏢 <b>Éditeur :</b> {html.escape(publisher)}")

    lines.append(f"🎯 <b>Prix max :</b> {price_str}")
    lines.append(f"🏷️ <b>États :</b> {html.escape(cond_display)}")
    lines.append(f"🔑 <b>Inclus :</b> {_join_escaped(include_keywords)}")
    if variants:
        lines.append(f"✨ <b>Variantes :</b> {_join_escaped(variants)}")
    if exclude_keywords:
        lines.append(f"🚫 <b>Exclus :</b> {_join_escaped(exclude_keywords)}")

    edition_lines = format_edition_line(edition_alternatives, isbn)
    if edition_lines:
        lines.extend(["", *edition_lines])

    # Le pied de confirmation est porté par la carte elle-même : tous les chemins qui
    # aboutissent ici finissent sur le même clavier, donc une seule formulation, et
    # aucune ne peut la laisser de côté.
    lines.extend(["", "Confirmer la création de cette alerte ?"])
    lines.append(
        "\n<i>Clique sur « Créer l'alerte » pour valider "
        "ou « Modifier » pour ajuster.</i>"
    )
    return "\n".join(lines)


def build_edition_choices(resolution: Any) -> list[dict[str, Any]]:
    """Éditions d'une résolution BNF, converties en brouillon stockable dans le FSM."""
    if resolution is None:
        return []
    return [edition.model_dump() for edition in resolution.editions()]


def _edition_button_text(edition: dict[str, Any]) -> str:
    """« Çà et là - 2022 » : l'éditeur d'abord, puis le millésime.

    L'éditeur prime parce qu'il distingue le plus souvent les éditions concurrentes
    d'une même œuvre (« Éditions poches » contre « Album ») ; le millésime le suit
    quand la BNF le donne.
    """
    publisher = str(edition.get("publisher") or "").strip()
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
        isbn_note=data.get("isbn_note"),
        edition_alternatives=data.get("edition_alternatives"),
    )


def creation_keyboard(
    data: dict[str, Any],
    show_variants_btn: bool = True,
) -> InlineKeyboardMarkup:
    """Clavier du récap : éditions numérotées puis validation."""
    alternatives = data.get("edition_alternatives")
    return get_alert_confirmation_keyboard(
        show_variants_btn=show_variants_btn,
        edition_alternatives=alternatives,
        edition_labels=build_edition_labels(alternatives),
    )


def format_alerts_list_text(
    alerts: list[Alert],
    current_page: int,
    total_count: int,
    category_label: str | None = None,
) -> str:
    """Génère le texte de la vue compacte /list."""
    total_pages = max(1, (total_count + 9) // 10)
    title_suffix = f" — {category_label}" if category_label else ""
    header = (
        f"📋 <b>Vos alertes surveillées{title_suffix}</b> "
        f"(Page {current_page + 1}/{total_pages} — "
        f"{total_count} alerte{'s' if total_count > 1 else ''}) :\n"
    )
    lines = [header]
    start_num = current_page * 10 + 1
    for i, a in enumerate(alerts, start=start_num):
        status_icon = "🟢" if a.status == AlertStatus.ACTIVE else "⏸️"
        cat_icon = (
            f"{a.category.emoji} "
            if getattr(a, "category", None) and getattr(a.category, "emoji", None)
            else ""
        )
        price_str = format_cents_to_euros(a.max_item_price_cents)
        if getattr(a, "isbn", None):
            lines.append(
                f"<b>{i}.</b> {status_icon} {cat_icon}<b>{html.escape(a.name)}</b>\n"
                f"   ↳ ISBN : <code>{a.isbn}</code> • Max : {price_str}"
            )
        else:
            lines.append(
                f"<b>{i}.</b> {status_icon} {cat_icon}<b>{html.escape(a.name)}</b>\n"
                f"   ↳ Max : {price_str}"
            )
    lines.append("\n<i>Cliquez sur un numéro ci-dessous pour ouvrir l'alerte :</i>")
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
    return build_edition_choices(resolution)


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

    status_msg = await target_msg.answer("🔍 Recherche automatique de l'œuvre...")

    service = BookMetadataService()
    meta = await service.lookup(clean_query)

    if not meta:
        await status_msg.edit_text(
            f"❌ Aucune œuvre trouvée pour « <b>{html.escape(clean_query)}</b> ».\n\n"
            "Tu peux réessayer avec un autre titre ou nom d'auteur :"
        )
        await state.set_state(AlertCreationState.waiting_for_query)
        return

    # GARDE ANTI-DOUBLON : Vérifier si l'article est déjà dans les alertes de l'utilisateur
    if db_user:
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(db_user.id, isbn=meta.isbn13, title=meta.title)
        if existing:
            status_emoji = "🟢" if existing.status == AlertStatus.ACTIVE else "⏸️"
            status_text = "Active" if existing.status == AlertStatus.ACTIVE else "En pause"
            price_str = (
                f"{existing.max_item_price_cents / 100:.2f} €".replace(".", ",")
                if existing.max_item_price_cents
                else "Aucun"
            )

            # Cas 1 : L'utilisateur avait déjà spécifié un prix en ligne (ex: /add deathbringer 40)
            if inline_price_cents is not None:
                new_price_str = f"{inline_price_cents / 100:.2f} €".replace(".", ",")
                await state.update_data(
                    existing_alert_id=str(existing.id),
                    new_max_price_cents=inline_price_cents,
                )
                warn_text = (
                    "⚠️ <b>Cette BD est déjà surveillée dans tes alertes !</b>\n\n"
                    f"📚 <b>{html.escape(existing.name)}</b>\n"
                    f"🔢 <b>ISBN :</b> <code>{existing.isbn or meta.isbn13 or 'N/A'}</code>\n"
                    f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                    f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                    f"💡 <i>Tu as indiqué un prix de <b>{new_price_str}</b>.</i>\n"
                    "Souhaites-tu mettre à jour le prix max de cette alerte existante ?"
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"🔄 Mettre à jour à {new_price_str}",
                                callback_data=f"upd_alert_price:{existing.id}:{inline_price_cents}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Conserver l'existante",
                                callback_data="alert_create_cancel",
                            )
                        ],
                    ]
                )
                await status_msg.edit_text(warn_text, reply_markup=keyboard)
                return

            # Cas 2 : Pas de prix spécifié (ex: /add deathbringer)
            await state.update_data(existing_alert_id=str(existing.id))
            await state.set_state(AlertCreationState.waiting_for_max_price)
            warn_text = (
                "⚠️ <b>Cette BD est déjà surveillée dans tes alertes !</b>\n\n"
                f"📚 <b>{html.escape(existing.name)}</b>\n"
                f"🔢 <b>ISBN :</b> <code>{existing.isbn or meta.isbn13 or 'N/A'}</code>\n"
                f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                "ℹ️ <i>Cette œuvre est déjà surveillée en continu 24h/24.</i>\n\n"
                "💶 <b>Pour modifier son prix max, entre un nouveau montant "
                "(ou clique sur Annuler) :</b>"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Conserver sans modification",
                            callback_data="alert_create_cancel",
                        )
                    ]
                ]
            )
            await status_msg.edit_text(warn_text, reply_markup=keyboard)
            return

    # Catégorie BD & Comics par défaut
    category_repo = _get_category_repo(db_session)
    category = await category_repo.get_by_slug(CategorySlug.BD_COMICS.value)
    cat_id_str = str(category.id) if category else ""

    edition_choices = await _resolve_edition_choices(meta.title, meta.author)

    await state.update_data(
        category_id=cat_id_str,
        category_slug=CategorySlug.BD_COMICS.value,
        name=meta.title,
        isbn=meta.isbn13,
        author=meta.author,
        publisher=meta.publisher,
        include_keywords=meta.recommended_includes,
        exclude_keywords=meta.recommended_excludes,
        variants=meta.recommended_variants,
        selected_conditions=list(DEFAULT_BD_CONDITIONS),
        edition_alternatives=edition_choices,
    )

    if inline_price_cents is not None:
        await state.update_data(max_item_price_cents=inline_price_cents)
        await state.set_state(AlertCreationState.confirm_creation)
        draft = await state.get_data()
        await status_msg.edit_text(
            recap_from_state({**draft, "max_item_price_cents": inline_price_cents}),
            reply_markup=creation_keyboard(draft),
        )
        return

    # Demander le prix max
    await state.set_state(AlertCreationState.waiting_for_max_price)
    prompt = (
        f"📚 <b>{html.escape(meta.title)}</b>\n"
        f"✍️ <b>Auteur :</b> {html.escape(meta.author or 'Non spécifié')}\n"
        f"🏢 <b>Éditeur :</b> {html.escape(meta.publisher or 'Non spécifié')}\n"
        f"🔢 <b>ISBN :</b> <code>{meta.isbn13 or 'Non trouvé'}</code>\n\n"
        f"🔑 <b>Inclus :</b> {', '.join(meta.recommended_includes)}\n"
        f"🚫 <b>Exclus :</b> {', '.join(meta.recommended_excludes)}\n"
        "💎 <b>États :</b> Neuf avec étiquette, Neuf sans étiquette, Très bon état\n\n"
        "💶 <b>Indique ton prix maximum pour cette alerte (en €)</b> <i>(ou /cancel)</i> :"
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
            status_emoji = "🟢" if existing.status == AlertStatus.ACTIVE else "⏸️"
            status_text = "Active" if existing.status == AlertStatus.ACTIVE else "En pause"
            price_str = (
                f"{existing.max_item_price_cents / 100:.2f} €".replace(".", ",")
                if existing.max_item_price_cents
                else "Aucun"
            )

            # Cas 1 : L'utilisateur a spécifié un nouveau prix inline
            if inline_price_cents is not None:
                new_price_str = f"{inline_price_cents / 100:.2f} €".replace(".", ",")
                await state.update_data(
                    existing_alert_id=str(existing.id),
                    new_max_price_cents=inline_price_cents,
                )
                warn_text = (
                    f"⚠️ <b>Ce produit ({cat_label}) est déjà surveillé dans tes alertes !</b>\n\n"
                    f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n"
                    f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                    f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                    f"💡 <i>Tu as indiqué un prix de <b>{new_price_str}</b>.</i>\n"
                    "Souhaites-tu mettre à jour le prix max de cette alerte existante ?"
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"🔄 Mettre à jour à {new_price_str}",
                                callback_data=f"upd_alert_price:{existing.id}:{inline_price_cents}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Conserver l'existante",
                                callback_data="alert_create_cancel",
                            )
                        ],
                    ]
                )
                if (
                    isinstance(event, CallbackQuery)
                    and event.message
                    and hasattr(event.message, "edit_text")
                ):
                    await event.message.edit_text(warn_text, reply_markup=keyboard)
                else:
                    await target_msg.answer(warn_text, reply_markup=keyboard)
                return

            # Cas 2 : Pas de prix inline spécifié
            await state.update_data(existing_alert_id=str(existing.id))
            await state.set_state(AlertCreationState.waiting_for_max_price)
            warn_text = (
                f"⚠️ <b>Ce produit ({cat_label}) est déjà surveillé dans tes alertes !</b>\n\n"
                f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n\n"
                f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                "💶 <b>Pour modifier son prix max, entre un nouveau montant "
                "(ou clique sur Annuler) :</b>"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Conserver sans modification",
                            callback_data="alert_create_cancel",
                        )
                    ]
                ]
            )
            if (
                isinstance(event, CallbackQuery)
                and event.message
                and hasattr(event.message, "edit_text")
            ):
                await event.message.edit_text(warn_text, reply_markup=keyboard)
            else:
                await target_msg.answer(warn_text, reply_markup=keyboard)
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
        recap_text = recap_from_state({**draft, "max_item_price_cents": inline_price_cents})
        keyboard = creation_keyboard(draft)
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
        "💶 <b>Indique ton prix maximum pour cette alerte (en €)</b> <i>(ou /cancel)</i> :"
    )
    if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
        await event.message.edit_text(prompt)
    else:
        await target_msg.answer(prompt)
