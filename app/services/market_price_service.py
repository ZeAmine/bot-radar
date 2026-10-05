"""Service de comparaison instantanée des prix de marché d'occasion.

Deux rendus, tous deux **synchrones et sans aucun appel réseau** (invariant n°1 :
l'alerte Telegram part avant toute requête) :

- `get_comparison_summary` : la ligne compacte affichée sous l'annonce dans l'alerte ;
- `format_market_details_card` : le panneau détaillé du bouton « 📊 Prix du marché ».

Référence de prix, règle unique appliquée partout : le **prix le plus bas que
nous puissions constater** pour cet ouvrage s'il est connu, sinon le **prix du
marché du jour**. Ce plus bas est borné par nos propres relevés : une cotation
agrégateur supérieure à un prix livré que nous avons vu pour le même livre
serait une cote fausse. L'écart affiché compare toujours le total Vinted
(article + protection + port) à cette référence.
"""

import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.core.constants import ComparisonStatus
from app.core.url_validator import is_safe_external_url
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.listing import Listing
from app.domain.rules.market_offer import (
    is_brand_new_partner,
    is_merchant_excluded,
    is_very_good_or_better,
    normalize_secondhand_condition,
)
from app.services.price_calculator import calculate_estimated_total
from app.utils.price import format_cents

if TYPE_CHECKING:
    from app.services.tome_extractor import TomeMatchResult

# Nombre de boutiques comparées au maximum dans le panneau détaillé.
MAX_PARTNERS_SHOWN = 5

# Ligne affichée quand le livre est identifié mais pas encore côté : mieux vaut
# dire que la cote arrive que laisser croire qu'il n'y a pas de prix.
PENDING_COTE_LINE = "Cote d'occasion en cours de calcul…"

try:
    _PARIS_TZ: ZoneInfo = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover - fuseau absent sur l'image de prod
    _PARIS_TZ = UTC  # type: ignore[assignment]


def _positive(value: int | float | None) -> int | None:
    """Normalise un prix stocké : entier strictement positif, sinon ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    cents = int(value)
    return cents if cents > 0 else None


def clean_tome_display_title(title: str, include_subtitle: bool = False) -> str:
    """Nettoie le titre du tome pour affichage.

    Si include_subtitle est True, conserve le sous-titre entre parenthèses s'il existe.
    """
    if not title:
        return ""
    cleaned = title.strip()

    # 1. Détection explicite du Tome / Volume / Intégrale
    tome_match = re.search(
        r"\b(Tome\s+\d+|Volume\s+\d+|Intégrale\b[^\-\:\(]*)",
        cleaned,
        re.IGNORECASE,
    )
    if tome_match:
        tome_label = tome_match.group(0).strip().capitalize()
        if not include_subtitle:
            return tome_label

        # Recherche du sous-titre entre parenthèses
        m_paren = re.search(r"\((.+)\)\s*$", cleaned)
        if m_paren:
            sub = m_paren.group(1).strip()
            # Nettoyer les répétitions inutiles comme 'Tome 1' ou 'Volume 1'
            clean_sub = re.sub(r"\s*\(.*?\)", "", sub).strip()
            if clean_sub.lower() == tome_label.lower():
                extra = re.search(r"\((.*?)\)", sub)
                if extra and extra.group(1).strip():
                    return f"{tome_label} ({extra.group(1).strip()})"
                return tome_label
            return f"{tome_label} ({sub})"
        return tome_label

    # 2. Livre sans mention de tome (titre unitaire ou série non numérotée)
    if not include_subtitle:
        no_paren = re.sub(r"\s*\([^)]*\)", "", cleaned).strip()
        while "(" in no_paren and ")" in no_paren:
            no_paren = re.sub(r"\s*\([^)]*\)", "", no_paren).strip()
        if "—" in no_paren:
            return no_paren.split("—")[-1].strip()
        if " - " in no_paren:
            return no_paren.split(" - ")[-1].strip()
        return no_paren

    # include_subtitle = True sans numéro de tome
    if "—" in cleaned:
        res = cleaned.split("—", 1)[-1].strip()
    elif " - " in cleaned:
        res = cleaned.split(" - ", 1)[-1].strip()
    else:
        res = cleaned

    if ")" in res and "(" not in res:
        res = res.replace(")", "").strip()
    if "(" in res and ")" not in res:
        res = res.replace("(", "").strip()
    return res


def extract_tome_number_from_title(title: str | None) -> int | None:
    """Extrait le numéro de tome ou volume s'il existe dans le titre."""
    if not title:
        return None
    m = re.search(r"\b(?:tome|t|vol|volume)\s*\.?\s*(\d+)\b", title, re.IGNORECASE)
    return int(m.group(1)) if m else None


def reference_price(cache: BookMarketCache | None) -> tuple[int | None, bool]:
    """Prix de référence d'un livre et nature de cette référence.

    Le prix le plus bas constaté sur 1 an prime toujours : c'est le « meilleur prix
    connu » de l'ouvrage. Le prix du marché du jour ne sert que de repli quand
    aucun historique certifié n'a encore été collecté.

    Returns:
        tuple (prix en centimes ou ``None``, ``True`` si l'historique 1 an est la source)
    """
    if cache is None:
        return None, False
    historical_low = _positive(cache.historical_low_1y_cents)
    if historical_low is not None:
        return historical_low, True
    return _positive(cache.lowest_delivered_price_cents), False


def has_usable_price(cache: BookMarketCache | None) -> bool:
    """Vrai si le cache suffit à comparer une annonce à un prix de référence.

    Une cote ``UNAVAILABLE`` sans historique 1 an ne l'est pas : elle dit qu'une
    passe n'a rien trouvé, pas que l'ouvrage n'a aucun prix.
    """
    reference_cents, _ = reference_price(cache)
    return reference_cents is not None


def format_cote_lines(book: Book | None) -> list[str]:
    """Lignes de cote déjà connues d'un livre, pour un message qui n'attend rien.

    Aucun prix n'est inventé : une ligne absente signifie « inconnu », et c'est
    à l'appelant de décider s'il doit l'annoncer ou attendre la cotation.

    Le cache doit être chargé : `BookRepository` le fait par défaut, et une
    lecture paresseuse ici lèverait au lieu de renvoyer une ligne.
    """
    cache = book.market_cache if book is not None else None
    if cache is None:
        return []

    lines: list[str] = []
    market_cents = _positive(cache.lowest_delivered_price_cents)
    if market_cents is not None:
        lines.append(f"📊 <b>Prix du marché :</b> {format_cents(market_cents)}")
    historical_cents = _positive(cache.historical_low_1y_cents)
    if historical_cents is not None:
        lines.append(
            f"📉 <b>Prix bas constaté :</b> {format_cents(historical_cents)}"
        )
    return lines


def _format_checked_at(moment: datetime | None) -> str:
    """Horodatage local lisible, utilisé comme pied de panneau."""
    if moment is None:
        return "récemment"
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(_PARIS_TZ).strftime("%d/%m à %H:%M")


def _vinted_total_cents(listing: Listing) -> int | None:
    """Total payé sur Vinted, frais de port et de protection inclus.

    Le total déjà calculé est prioritaire ; à défaut, le barème standard Vinted est
    appliqué au prix de l'annonce (invariant : une offre sans port n'est jamais
    comparée à un prix article seul).
    """
    total = _positive(listing.estimated_total_cents)
    if total is not None:
        return total
    item_price = _positive(listing.item_price_cents)
    if item_price is None:
        return None
    return _positive(calculate_estimated_total(item_price, fallback_defaults=True))


def _format_gap(total_cents: int, reference_cents: int) -> str:
    """Ligne d'écart entre le total Vinted et le prix de référence."""
    diff_cents = total_cents - reference_cents
    diff_euros = format_cents(abs(diff_cents))
    if diff_cents < 0:
        return f"✅ Écart : <b>-{diff_euros}</b>"
    if diff_cents > 0:
        return f"⛔️ Écart : <b>+{diff_euros}</b>"
    return "✅ Écart : <b>0,00 €</b> (au prix de référence)"


def _match_lot_books(books: list[Book], lot_tomes: list[int]) -> list[Book]:
    """Retient, dans une série, les livres correspondant aux tomes d'un lot."""
    return [
        book
        for book in books
        if (tome := extract_tome_number_from_title(book.title)) is not None
        and tome in lot_tomes
    ]


def _lot_label(
    expected_count: int,
    priced_count: int,
    *,
    composition_inferred: bool,
) -> str:
    """Parenthésé de la ligne de cote d'un lot.

    Le libellé dit toujours sur quoi il repose : le vendeur a-t-il nommé les
    tomes, ou est-ce nous qui les avons composées du seul compte qu'il a donné ?
    Sans cette nuance, « (Lot) » ferait passer une déduction pour un inventaire,
    alors que « 4 tomes » face à une série de 12 ne dit rien de la composition.
    """
    if not composition_inferred:
        if priced_count < expected_count:
            return f" (Lot, {priced_count}/{expected_count} tomes)"
        return " (Lot)"
    coverage = f", {priced_count}/{expected_count}" if priced_count < expected_count else ""
    return f" (Lot de {expected_count} tomes, composition déduite{coverage})"


def _sort_series_books(books: list[Book]) -> list[Book]:
    """Trie une série par numéro de tome, puis par titre pour les livres sans tome."""
    return sorted(
        books,
        key=lambda book: (
            extract_tome_number_from_title(book.title) is None,
            extract_tome_number_from_title(book.title) or 0,
            book.title or "",
        ),
    )


def _extract_partners(
    cache: BookMarketCache,
    latest_snapshot: Any,
    snapshots: list[Any] | None,
) -> dict[str, Any]:
    """Retrouve le comparatif par boutique, du snapshot le plus frais au cache.

    Le snapshot du jour est prioritaire : il décrit les prix réellement en stock.
    `book.snapshots` n'est jamais chargé à la demande (chargement lazy incompatible
    avec asyncio) : seuls les snapshots déjà en mémoire sont considérés.
    """
    snapshot_payload = getattr(latest_snapshot, "raw_payload", None)
    if isinstance(snapshot_payload, dict):
        partners = snapshot_payload.get("partners")
        if isinstance(partners, dict):
            return partners

    for snapshot in sorted(
        snapshots or [], key=lambda snap: snap.checked_at or datetime.min, reverse=True
    ):
        payload = getattr(snapshot, "raw_payload", None)
        if isinstance(payload, dict):
            partners = payload.get("partners")
            if isinstance(partners, dict):
                return partners

    cache_payload = getattr(cache, "raw_payload", None)
    if isinstance(cache_payload, dict):
        partners = cache_payload.get("partners")
        if isinstance(partners, dict):
            return partners

    return {}


def _select_secondhand_partners(partners: dict[str, Any]) -> dict[str, Any]:
    """Filtre STRICT le comparatif : occasion, état au moins « Très bon », prix connu.

    Le Neuf et les états inférieurs ne doivent jamais apparaître comme cote.
    """
    selected: dict[str, Any] = {}
    for merchant, data in partners.items():
        if not isinstance(data, dict):
            continue
        if is_merchant_excluded(merchant) or is_brand_new_partner(data):
            continue
        total_cents = _positive(data.get("total_cents"))
        if total_cents is None or not is_very_good_or_better(data.get("condition")):
            continue
        selected[merchant] = data
    return selected


def _is_same_book(candidate: Book, reference: Book | None) -> bool:
    """Identifie le livre courant dans la liste des tomes de sa série."""
    if reference is None:
        return False
    if candidate is reference:
        return True
    return candidate.id is not None and candidate.id == reference.id


class MarketPriceService:
    """Service de consultation du cache de marché et de calcul d'écarts."""

    @staticmethod
    def get_comparison_summary(
        listing: Listing,
        book: Book | None,
        books_in_series: list[Book] | None = None,
        tome_result: "TomeMatchResult | None" = None,
    ) -> str | None:
        """Génère la ligne de comparaison épurée pour la légende Telegram."""
        books_in_series = books_in_series or []

        # 1. Livre précis résolu (tome unique ou one-shot)
        if book and book.market_cache:
            summary = MarketPriceService._single_book_summary(listing, book, tome_result)
            if summary is not None:
                return summary

        # 2. Lot de tomes identifiés dans une série
        if tome_result and tome_result.is_lot and tome_result.lot_tomes and books_in_series:
            summary = MarketPriceService._lot_summary(
                listing,
                books_in_series,
                tome_result.lot_tomes,
                composition_inferred=tome_result.composition_inferred,
            )
            if summary is not None:
                return summary

        # 3. Série reconnue mais tome non résolu. La ligne dit ce qui est su — la
        # série — et se tait sur ce qui ne l'est pas : « cote indisponible » y
        # annonçait une absence de prix alors que le panneau « 📊 Prix du marché »
        # en affiche la liste. Le prix n'a rien à faire sur une ligne qui ne sait
        # pas de quel tome elle parle.
        if len(books_in_series) > 1:
            return "ℹ️ Série détectée : tome non identifié."

        return None

    @staticmethod
    def _single_book_summary(
        listing: Listing,
        book: Book,
        tome_result: "TomeMatchResult | None",
    ) -> str | None:
        """Ligne de cote d'un tome unique, comparée au prix le plus bas connu."""
        cache = book.market_cache
        if cache is None:
            return None
        reference_cents, is_historical = reference_price(cache)

        # Sans historique certifié, un cache expiré ne peut plus rien garantir.
        if reference_cents is None:
            return None
        if not is_historical and MarketPriceService._is_expired(cache):
            return None
        if cache.comparison_status == ComparisonStatus.UNAVAILABLE and not is_historical:
            return None

        tome_suffix = (
            f" (T{tome_result.tome_number})"
            if (tome_result and tome_result.tome_number)
            else ""
        )
        lines = [
            f"📉 Prix bas constaté{tome_suffix} : {format_cents(reference_cents)}"
            if is_historical
            else MarketPriceService._market_price_line(cache, tome_suffix)
        ]

        vinted_total = _vinted_total_cents(listing)
        if vinted_total is not None:
            lines.append(_format_gap(vinted_total, reference_cents))

        return "\n".join(lines)

    @staticmethod
    def _is_expired(cache: BookMarketCache) -> bool:
        """Vrai si le cache de marché a dépassé sa durée de validité."""
        if cache.expires_at is None:
            return False
        expires_at = cache.expires_at
        aware = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        return aware < datetime.now(UTC)

    @staticmethod
    def _market_price_line(cache: BookMarketCache, tome_suffix: str = "") -> str:
        """Ligne de repli : prix du marché du jour, avec le marchand le moins cher."""
        provider = (cache.best_provider_name or "").replace("_", " ").title()
        provider_suffix = f" ({provider})" if provider else ""
        price = format_cents(_positive(cache.lowest_delivered_price_cents) or 0)
        return f"📊 Prix du marché{tome_suffix} : {price}{provider_suffix}"

    @staticmethod
    def _lot_summary(
        listing: Listing,
        books_in_series: list[Book],
        lot_tomes: list[int],
        composition_inferred: bool = False,
    ) -> str | None:
        """Cote cumulée d'un lot, avec la couverture réelle de l'appariement.

        Seuls les tomes effectivement appariés sont cumulés : un lot « T1-T3 »
        dont le T2 n'est pas au catalogue est annoncé comme tel plutôt que
        comparé à une somme qui n'inclut pas le tome manquant.
        """
        matched = _match_lot_books(books_in_series, lot_tomes)
        expected_count = len(set(lot_tomes))
        target_books = matched or books_in_series

        priced: list[tuple[int, bool]] = []
        for book in target_books:
            price_cents, is_historical = reference_price(book.market_cache)
            if price_cents is not None:
                priced.append((price_cents, is_historical))

        if not priced:
            return None

        cumulated = sum(price_cents for price_cents, _ in priced)
        all_historical = all(is_historical for _, is_historical in priced)
        label = "📉 Prix bas constaté" if all_historical else "📊 Prix du marché"
        label += _lot_label(
            expected_count,
            len(priced),
            composition_inferred=composition_inferred,
        )

        lines = [f"{label} : ~{format_cents(cumulated)}"]
        vinted_total = _vinted_total_cents(listing)
        if vinted_total is not None:
            lines.append(_format_gap(vinted_total, cumulated))
        return "\n".join(lines)

    @staticmethod
    def format_market_details_card(
        listing: Listing,
        book: Book | None,
        books_in_series: list[Book] | None = None,
        tome_result: "TomeMatchResult | None" = None,
        latest_snapshot: Any = None,
    ) -> str:
        """Génère le texte complet du panneau callback « Prix du marché »."""
        books_in_series = books_in_series or []

        # Résolution de repli : un tome non résolu mais une série d'un seul livre.
        if (
            not book
            and len(books_in_series) == 1
            and not (tome_result and tome_result.is_lot)
        ):
            book = books_in_series[0]

        if not book and books_in_series:
            if tome_result and tome_result.is_lot:
                return MarketPriceService._lot_card(books_in_series, tome_result.lot_tomes)
            return MarketPriceService._series_card(listing, books_in_series)

        return MarketPriceService._single_book_card(book, books_in_series, latest_snapshot)

    @staticmethod
    def _header(title: str) -> list[str]:
        return ["📊 <b>Prix du marché</b>\n", title]

    @staticmethod
    def _series_books_lines(
        books: list[Book],
        current_book: Book | None,
        fallback_price: int | None = None,
    ) -> list[str]:
        """Lignes « tome : prix » d'une série, tome courant mis en gras."""
        has_numbered_tomes = any(
            extract_tome_number_from_title(book.title) is not None for book in books
        )
        lines: list[str] = []
        for book in _sort_series_books(books):
            if has_numbered_tomes and extract_tome_number_from_title(book.title) is None:
                continue
            is_current = _is_same_book(book, current_book)
            price_cents, _ = reference_price(book.market_cache)
            if price_cents is None and is_current:
                price_cents = fallback_price
            if price_cents is None:
                continue
            entry = f"• {clean_tome_display_title(book.title)} : {format_cents(price_cents)}"
            lines.append(f"• <b>{entry[2:]}</b>" if is_current else entry)
        return lines

    @staticmethod
    def _latest_checked_at(books: list[Book]) -> str:
        checked_dates = [
            book.market_cache.checked_at
            for book in books
            if book.market_cache and book.market_cache.checked_at
        ]
        return _format_checked_at(max(checked_dates) if checked_dates else None)

    @staticmethod
    def _lot_card(books_in_series: list[Book], lot_tomes: list[int] | None) -> str:
        """Panneau d'un lot : cote de chaque tome identifié, jamais un cumul fictif."""
        matched = _match_lot_books(books_in_series, lot_tomes or [])
        target_books = matched or books_in_series

        lines = MarketPriceService._header("<b>Cote par tome (plus bas connu) :</b>")
        for book in _sort_series_books(target_books):
            price_cents, _ = reference_price(book.market_cache)
            if price_cents is None:
                continue
            lines.append(f"• {clean_tome_display_title(book.title)} : {format_cents(price_cents)}")

        latest = MarketPriceService._latest_checked_at(target_books)
        lines.append(f"\nDernière mise à jour : {latest}")
        return "\n".join(lines)

    @staticmethod
    def _series_card(listing: Listing, books_in_series: list[Book]) -> str:
        """Panneau d'une série sans tome résolu : les cotes de tous ses tomes."""
        vinted_total = _vinted_total_cents(listing)
        lines = MarketPriceService._header(
            f"🆕 Vinted : {format_cents(vinted_total) if vinted_total else 'à confirmer'}"
        )
        lines.append("Annonce sans tome précis détecté.\n")
        lines.append("<b>Cotes par tome (plus bas connu) :</b>")
        series_lines = MarketPriceService._series_books_lines(books_in_series, None)
        # Un tome sans cote est une cote en attente, pas une cote absente : un
        # titre suivi d'une section vide se lisait comme un panneau mort.
        lines.extend(series_lines or [PENDING_COTE_LINE])
        latest = MarketPriceService._latest_checked_at(books_in_series)
        lines.append(f"\nDernière mise à jour : {latest}")
        return "\n".join(lines)

    @staticmethod
    def _single_book_card(
        book: Book | None,
        books_in_series: list[Book],
        latest_snapshot: Any,
    ) -> str:
        """Panneau d'un tome résolu : cote 1 an, comparatif par boutique, série."""
        cache = book.market_cache if book else None
        reference_cents, is_historical = reference_price(cache)
        if cache is None or reference_cents is None:
            return MarketPriceService._no_market_price_card()

        snapshots = book.__dict__.get("snapshots")
        partners = _select_secondhand_partners(
            _extract_partners(
                cache, latest_snapshot, snapshots if isinstance(snapshots, list) else None
            )
        )

        lines = ["📊 <b>Prix du marché</b>\n"]
        if is_historical:
            lines.append(f"📉 Prix bas constaté : {format_cents(reference_cents)}")
            if not partners:
                lines.append(
                    "(Cote d'occasion basée sur le plus bas prix "
                    "que nous avons pu constater)"
                )

        if partners:
            if is_historical:
                lines.append("\n<b>Comparatif par boutique :</b>")
            else:
                lines.append("<b>Comparatif par boutique :</b>")
            for merchant, data in sorted(
                partners.items(), key=lambda item: _positive(item[1].get("total_cents")) or 0
            )[:MAX_PARTNERS_SHOWN]:
                total_cents = _positive(data.get("total_cents")) or 0
                condition = normalize_secondhand_condition(data.get("condition"))
                lines.append(f"• {merchant} : {format_cents(total_cents)} ({condition})")
        elif not is_historical:
            lines.append(
                "(Aucune offre d'occasion comparable en stock chez nos partenaires.)"
            )

        if len(books_in_series) > 1:
            series_lines = MarketPriceService._series_books_lines(
                books_in_series, book, reference_cents
            )
            if len(series_lines) > 1:
                lines.append("\n<b>Autres tomes de la série :</b>")
                lines.extend(series_lines)

        best_url = MarketPriceService._best_offer_url(cache, partners)
        if best_url:
            lines.append(f'\nLien : <a href="{best_url}">Consulter l\'offre</a>')

        lines.append(f"\nDernière mise à jour : {_format_checked_at(cache.checked_at)}")
        return "\n".join(lines)

    @staticmethod
    def _best_offer_url(cache: BookMarketCache, partners: dict[str, Any]) -> str | None:
        """Lien de l'offre la moins chère, échappé et restreint aux URL publiques."""
        candidates: list[str] = [
            str(data["url"])
            for _, data in sorted(
                partners.items(), key=lambda item: _positive(item[1].get("total_cents")) or 0
            )
            if data.get("url")
        ]
        if cache.best_source_url:
            candidates.append(str(cache.best_source_url))

        for url in candidates:
            is_safe, _ = is_safe_external_url(url)
            if is_safe:
                return html.escape(url, quote=True)
        return None

    @staticmethod
    def _no_market_price_card() -> str:
        return (
            "📊 <b>Prix du marché</b>\n\n"
            "Aucune offre d'occasion comparable n'a pu être trouvée "
            "pour ce titre sur les plateformes partenaires."
        )
