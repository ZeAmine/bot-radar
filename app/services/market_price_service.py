"""Comparaison locale : observations datées sur 365 jours, sinon prix courant valide.

Les agrégats non datés et anciens minima restent informatifs. Les fenêtres sont
partielles ; les sommes de références ne constituent pas une observation simultanée.
Les montants livrés et règles d'arrondi du comparateur sont inchangés.
"""

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.core.constants import CATEGORY_EMOJIS, CategorySlug, ComparisonStatus
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
COTE_UNAVAILABLE_LINE = "⚠️ Cote indisponible"
COTE_NOT_READY_LINE = f"{COTE_UNAVAILABLE_LINE} (aucun relevé enregistré à l’envoi)."
LOT_UNRESOLVED_LINE = "⚠️ Composition du lot non identifiée : comparaison indisponible."

# Ligne affichée quand aucun livre n'est encore identifié pour l'alerte : la
# résolution (catalogue, puis BNF) tourne en arrière-plan. Sans elle, l'alerte
# partait sans aucun bloc de cote, ce qui se lisait comme une absence de prix.
SEARCHING_BOOK_LINE = "🔎 Recherche de l'ouvrage en cours…"

# Ligne affichée quand un livre identifié a bel et bien été coté — une passe a eu
# lieu — mais qu'aucun marchand n'a produit d'offre. « En cours de calcul » serait
# faux : le calcul est fait, son résultat est l'absence d'offre. Le relevé est daté
# par `cote_absence_line` ; la phrase sert de préfixe (`is_no_offer_line`).
NO_OFFER_LINE = "🔍 Aucune offre d'occasion trouvée"

# Ligne affichée quand la recherche d'ouvrage a **conclu** : toutes les autorités
# interrogées ont répondu, aucune ne catalogue l'ouvrage. La ligne d'attente
# (« recherche de l'ouvrage en cours ») serait fausse : la recherche est finie, et
# son résultat est cette absence. Le verdict est persisté sur l'alerte (ADR-041).
NO_CATALOGUE_LINE = "🚫 Aucune source ne catalogue cet ouvrage — la veille reste par titre."

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
    """Projection documentée encore valide, sinon offre courante admissible."""
    if cache is None:
        return None, False
    now = datetime.now(UTC)
    annual = _positive(cache.annual_low_cents)
    dates = (
        cache.annual_computed_at,
        cache.annual_expires_at,
        cache.annual_low_observed_at,
        cache.annual_first_at,
        cache.annual_last_at,
    )
    if annual is not None and all(value is not None for value in dates):
        computed, expiry, observed, first, last = [_aware_checked_at(value) for value in dates]
        if (
            computed <= now < expiry
            and now - timedelta(days=365) < first <= observed <= last <= now
            and isinstance(cache.annual_count, int)
            and cache.annual_count > 0
            and cache.annual_low_snapshot_id is not None
            and cache.annual_low_provider
        ):
            return annual, True
    current = _positive(cache.lowest_delivered_price_cents)
    if (
        current is not None
        and cache.comparison_status == ComparisonStatus.AVAILABLE
        and cache.checked_at is not None
        and _aware_checked_at(cache.checked_at) <= now
        and not MarketPriceService._is_expired(cache)
    ):
        return current, False
    return None, False


def has_usable_price(cache: BookMarketCache | None) -> bool:
    """Vrai si le cache suffit à comparer une annonce à un prix de référence.

    Une cote ``UNAVAILABLE`` sans observation annuelle admissible ne l'est pas : elle dit qu'une
    passe n'a rien trouvé, pas que l'ouvrage n'a aucun prix.
    """
    reference_cents, _ = admissible_reference_price(cache)
    return reference_cents is not None


def admissible_reference_price(cache: BookMarketCache | None) -> tuple[int | None, bool]:
    """Même sélection documentée pour tous les rendus et totaux."""
    price, historical = reference_price(cache)
    if price is None or historical:
        return price, historical
    if cache is None or cache.comparison_status != ComparisonStatus.AVAILABLE:
        return None, False
    if MarketPriceService._is_expired(cache):
        return None, False
    return price, False


def _reference_reason(book: Book) -> str | None:
    cache = _loaded_market_cache(book)
    if has_usable_price(cache):
        return None
    if cache is not None and _positive(cache.lowest_delivered_price_cents) is not None:
        if cache.expires_at is None:
            return "validité du prix courant inconnue"
        if MarketPriceService._is_expired(cache):
            return "prix courant expiré"
    if cache is not None and cache.historical_low_1y_cents is not None:
        return "ancien minimum non vérifiable sur 365 jours"
    return "cote indisponible"


@dataclass(frozen=True)
class LotReference:
    """Résolution locale commune aux deux rendus : une édition par tome, toutes admissibles."""

    books: list[Book]
    total_cents: int | None
    historical: bool
    reason: str | None


def lot_reference(books: list[Book], wanted: list[int]) -> LotReference:
    requested = set(wanted)
    matched = _match_lot_books(books, wanted)
    if not requested:
        return LotReference([], None, False, "composition non identifiée")
    by_number: dict[int, list[Book]] = {number: [] for number in requested}
    for candidate in matched:
        number = extract_tome_number_from_title(candidate.title)
        if number is not None:
            by_number[number].append(candidate)
    if any(len(values) > 1 for values in by_number.values()):
        return LotReference(matched, None, False, "éditions ambiguës pour les tomes demandés")
    if any(not values for values in by_number.values()):
        found = sum(bool(values) for values in by_number.values())
        return LotReference(
            matched,
            None,
            False,
            f"référence incomplète ({found}/{len(requested)} tomes identifiés)",
        )
    reasons = [_reference_reason(candidate) for candidate in matched]
    if any(reasons):
        return LotReference(
            matched, None, False, "; ".join(dict.fromkeys(reason for reason in reasons if reason))
        )
    prices = [admissible_reference_price(_loaded_market_cache(candidate)) for candidate in matched]
    return LotReference(
        matched,
        sum(price for price, _ in prices if price is not None),
        all(historical for _, historical in prices),
        None,
    )


def _reference_detail(book: Book) -> str:
    cache = _loaded_market_cache(book)
    price, historical = reference_price(cache)
    reason = _reference_reason(book)
    value = format_cents(price) if price is not None else "cote indisponible"
    qualifier = f" ({reason})" if reason and price is not None else ""
    line = f"• {clean_tome_display_title(book.title)} : {value}{qualifier}"
    if cache is not None and historical:
        line += f" — minimum documenté ({_format_annual_date(cache.annual_low_observed_at)})"
        line += f"\n{annual_coverage(cache)}"
    elif price is not None:
        line += " — prix courant"
    return line


def _lot_unavailable(reference: LotReference) -> str:
    reason = reference.reason or "composition non identifiée"
    lines = [f"⚠️ {reason.capitalize()} : comparaison indisponible."]
    books = _sort_series_books(reference.books)
    # Le résumé reste compact ; le panneau garde les détails de tous les tomes.
    lines.extend(_reference_detail(book) for book in books[:3])
    if len(books) > 3:
        lines.append("… autres détails dans « Prix du marché ».")
    return "\n".join(lines)


def _loaded_market_cache(book: Book) -> BookMarketCache | None:
    """Cache de marché déjà chargé, sans déclencher de lecture paresseuse.

    Un livre rendu par `add_book` n'a pas sa relation chargée : la lire ici
    lèverait en session asyncio. « Pas chargé » se lit comme « pas encore connu »,
    donc comme une cote à venir — jamais comme une absence d'offre affirmée.
    """
    cache = book.__dict__.get("market_cache")
    return cache if isinstance(cache, BookMarketCache) else None


def is_no_offer_line(line: str | None) -> bool:
    """Vrai pour la ligne « aucune offre trouvée », comparée par préfixe.

    La ligne porte la date de son relevé : c'est cette phrase-ci qui l'identifie.
    """
    return line is not None and line.startswith(NO_OFFER_LINE)


def cote_absence_line(book: Book | None) -> str | None:
    """Ligne à annoncer quand aucune cote chiffrée n'est disponible pour ce livre.

    Deux silences, deux vérités, et une seule ligne pour les dire : un livre jamais
    coté attend encore sa passe (« en cours de calcul »), un livre dont la cote est
    vide **après** une passe a un résultat, et ce résultat est l'absence d'offre —
    daté du relevé qui l'a constaté. Les confondre faisait lire une attente
    éternelle là où la réponse était déjà connue.

    ``None`` quand il n'y a aucun livre : rien à dire d'une cote sans objet.
    """
    if book is None:
        return None
    cache = _loaded_market_cache(book)
    if (cache is not None and not has_usable_price(cache)
            and (_positive(cache.lowest_delivered_price_cents) is not None
                 or cache.historical_low_1y_cents is not None
                 or cache.annual_low_cents is not None)):
        reason = _reference_reason(book) or "cote indisponible"
        return f"{COTE_UNAVAILABLE_LINE} ({reason})."
    if cache is not None and cache.collection_status == "failed":
        date = _format_checked_at(cache.last_attempt_at)
        return f"{COTE_UNAVAILABLE_LINE} (tentative du {date})."
    if cache is None or cache.checked_at is None:
        return PENDING_COTE_LINE
    return f"{NO_OFFER_LINE} (relevé du {_format_checked_at(cache.checked_at)})."


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
        label = ("Prix du marché" if reference_price(cache) == (market_cents, False)
                 else "Dernière offre connue (informative)")
        lines.append(f"<b>{label} :</b> {format_cents(market_cents)}")
    historical_cents, historical = reference_price(cache)
    if historical and historical_cents is not None:
        lines.append(f"<b>Minimum des relevés sur 365 jours :</b> {format_cents(historical_cents)}")
        lines.append(annual_coverage(cache))
    return lines


def _aware_checked_at(moment: datetime | None) -> datetime:
    """Normalise les relevés SQLite et Postgres avant une comparaison de dates."""
    if moment is None:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _format_checked_at(moment: datetime | None) -> str:
    """Horodatage local lisible, utilisé comme pied de panneau."""
    if moment is None:
        return "Non renseignée"
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
    """Ligne d'écart entre le total Vinted et le prix de référence.

    Le libellé est gras, la valeur en romain (charte), et l'emoji qui porte le
    sens du signe reste hors du gras : il n'a pas à être mis en gras avec lui.

    L'emoji est en **fin** de ligne, pas en tête. Sur iOS les métriques
    verticales d'un emoji le poussent au-dessus de la ligne de texte ; en tête
    de ligne ce décalage se lit comme une puce de titre qui qualifierait tout le
    bloc, alors qu'il ne concerne que cet écart. En fin de ligne il ne se voit
    plus. Et si la ligne se replie sur un écran étroit, l'emoji seul sur la
    deuxième ligne se lit encore comme la conclusion de la ligne précédente.

    À l'écart nul, aucun emoji : « au prix de référence » porte déjà le sens,
    et un marqueur vert devant un écart nul se lit comme une approbation.
    """
    diff_cents = total_cents - reference_cents
    diff_euros = format_cents(abs(diff_cents))
    if diff_cents < 0:
        return f"<b>Écart :</b> -{diff_euros} ✅"
    if diff_cents > 0:
        return f"<b>Écart :</b> +{diff_euros} ⛔️"
    return "<b>Écart :</b> 0,00 € (au prix de référence)"


def _match_lot_books(books: list[Book], lot_tomes: list[int]) -> list[Book]:
    """Retient, dans une série, les livres correspondant aux tomes d'un lot."""
    return [
        book
        for book in books
        if (tome := extract_tome_number_from_title(book.title)) is not None and tome in lot_tomes
    ]


def listable_series_books(books: list[Book]) -> list[Book]:
    """Tomes qui figurent réellement dans le déroulé d'une série.

    Dès qu'un tome porte un numéro, une fiche sans numéro (intégrale, hors-série)
    double la série entière : elle disparaît du déroulé — et du total, qui doit
    sommer exactement ce que l'écran montre.
    """
    numbered = [b for b in books if extract_tome_number_from_title(b.title) is not None]
    return numbered if numbered else list(books)


def series_reference_total(books: list[Book]) -> int | None:
    """Total de série : somme des prix de référence des tomes listables.

    La somme ne porte que sur ce qui est connu — une somme vide n'est pas un
    total mais l'absence d'un total, d'où ``NULL``. C'est cette fonction, et elle
    seule, qui définit le total : `persist_market_result` l'appelle à chaque
    écriture de cote, et l'écran affiche la valeur ainsi persistée.
    """
    listed = listable_series_books(books)
    numbers = [extract_tome_number_from_title(book.title) for book in listed]
    numbered = [number for number in numbers if number is not None]
    if len(numbered) != len(set(numbered)):
        return None
    known = [
        price_cents
        for book in listed
        if (price_cents := admissible_reference_price(_loaded_market_cache(book))[0]) is not None
    ]
    return sum(known) if known else None


def _unpriced_tome_lines(books: list[Book]) -> str | None:
    """Comptes des tomes sans prix : l'attente et l'absence se distinguent.

    Cacher un tome muet faisait lire une série de 7 tomes comme une série de 5 —
    le silence passait pour l'inventaire complet de la série. Et compter pour « en
    cours » un tome dont une passe a déjà conclu à l'absence d'offre faisait
    attendre une cote qui n'arriverait pas : chaque silence a son compte.
    """
    pending = absence = unavailable = 0
    for book in listable_series_books(books):
        if has_usable_price(_loaded_market_cache(book)):
            continue
        line = cote_absence_line(book)
        if line and line.startswith(COTE_UNAVAILABLE_LINE):
            unavailable += 1
        elif is_no_offer_line(line):
            absence += 1
        else:
            pending += 1
    lines: list[str] = []
    if pending:
        lines.append(f"⏳ {pending} tome(s) en cours de cote…")
    if absence:
        lines.append(f"🔍 {absence} tome(s) sans aucune offre trouvée.")
    if unavailable:
        lines.append(f"⚠️ {unavailable} tome(s) sans cote disponible.")
    return "\n".join(lines) or None


def _series_total_line(total_cents: int, books: list[Book]) -> str:
    """Ligne « Total de série », avec la couverture réelle des tomes cotés."""
    listed = listable_series_books(books)
    priced = sum(1 for book in listed if has_usable_price(_loaded_market_cache(book)))
    coverage = f" ({priced}/{len(listed)} tomes)" if priced < len(listed) else ""
    references = [admissible_reference_price(_loaded_market_cache(book)) for book in listed]
    kinds = {historical for price, historical in references if price is not None}
    coverage += (
        " — références mixtes, dates distinctes"
        if len(kinds) > 1
        else " — références individuelles, dates distinctes"
    )
    return f"<b>Total de la série :</b> ~{format_cents(total_cents)}{coverage}"


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
    if cache.current_offers is not None:
        current: dict[str, Any] = {}
        names: dict[str, str] = {}
        for offer in cache.current_offers:
            payload = offer.get("raw_payload")
            if isinstance(payload, dict) and isinstance(payload.get("partners"), dict):
                candidates = payload["partners"]
            else:
                candidates = {
                    str(offer.get("provider_name", "Marchand")): {
                        "total_cents": offer.get("delivered_price_cents"),
                        "condition": offer.get("item_condition"),
                        "url": offer.get("source_url"),
                    }
                }
            for merchant, data in _select_secondhand_partners(candidates).items():
                key = merchant.strip().casefold()
                label = names.setdefault(key, merchant)
                previous = current.get(label)
                if previous is None or data["total_cents"] < previous["total_cents"]:
                    current[label] = data
        return current
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
        if book and book.market_cache and not (tome_result and tome_result.is_lot):
            summary = MarketPriceService._single_book_summary(listing, book, tome_result)
            if summary is not None:
                return summary

        # 2. Lot de tomes identifiés dans une série
        if tome_result and tome_result.is_lot and tome_result.lot_tomes:
            summary = MarketPriceService._lot_summary(
                listing,
                books_in_series,
                tome_result.lot_tomes,
                composition_inferred=tome_result.composition_inferred,
            )
            if summary is not None:
                return summary

        # Un lot non résolu n’est pas une cote en cours : son contenu manque.
        if tome_result and tome_result.is_lot:
            if not tome_result.lot_tomes:
                return LOT_UNRESOLVED_LINE
            matched = _match_lot_books(books_in_series, tome_result.lot_tomes)
            if not matched:
                return LOT_UNRESOLVED_LINE
            absences = [cote_absence_line(candidate) for candidate in matched]
            if any(line and line.startswith(COTE_UNAVAILABLE_LINE) for line in absences):
                return COTE_UNAVAILABLE_LINE
            if PENDING_COTE_LINE in absences:
                return PENDING_COTE_LINE
            return next((line for line in absences if line), COTE_UNAVAILABLE_LINE)

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
        reference_cents, is_historical = admissible_reference_price(cache)

        # Sans historique certifié, un cache expiré ne peut plus rien garantir.
        if reference_cents is None:
            return None
        if not is_historical and MarketPriceService._is_expired(cache):
            return None
        if cache.comparison_status == ComparisonStatus.UNAVAILABLE and not is_historical:
            return None

        tome_suffix = (
            f" (T{tome_result.tome_number})" if (tome_result and tome_result.tome_number) else ""
        )
        lines = [
            f"<b>Minimum des relevés sur 365 jours{tome_suffix} :</b> "
            f"{format_cents(reference_cents)}\n"
            f"{annual_coverage(cache)}"
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
            return True
        expires_at = cache.expires_at
        aware = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        return aware < datetime.now(UTC)

    @staticmethod
    def _market_price_line(cache: BookMarketCache, tome_suffix: str = "") -> str:
        """Ligne de repli : prix du marché du jour, avec le marchand le moins cher."""
        provider = (cache.best_provider_name or "").replace("_", " ").title()
        provider_suffix = f" ({provider})" if provider else ""
        price = format_cents(_positive(cache.lowest_delivered_price_cents) or 0)
        return f"<b>Prix du marché{tome_suffix} :</b> {price}{provider_suffix}"

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
        reference = lot_reference(books_in_series, lot_tomes)
        if reference.total_cents is None:
            return _lot_unavailable(reference)
        cumulated = reference.total_cents
        kinds = {
            admissible_reference_price(_loaded_market_cache(book))[1] for book in reference.books
        }
        expected_count = len(set(lot_tomes))
        label = (
            "Somme des minima documentés (dates distinctes)"
            if reference.historical
            else "Référence du lot"
        )
        if len(kinds) > 1:
            label += " (références mixtes, dates distinctes)"
        label += _lot_label(
            expected_count,
            len(reference.books),
            composition_inferred=composition_inferred,
        )

        lines = [f"<b>{label} :</b> ~{format_cents(cumulated)}"]
        vinted_total = _vinted_total_cents(listing)
        if vinted_total is not None:
            lines.append(_format_gap(vinted_total, cumulated))
        return "\n".join(lines)

    @staticmethod
    def format_market_details_card(
        listing: Listing | None,
        book: Book | None,
        books_in_series: list[Book] | None = None,
        tome_result: "TomeMatchResult | None" = None,
        latest_snapshot: Any = None,
        series_total_cents: int | None = None,
        catalogue_exhausted: bool = False,
        refresh_note: str | None = None,
        additional_note: str | None = None,
    ) -> str:
        """Génère le texte complet du panneau callback « Prix du marché ».

        `series_total_cents` est le total **persisté** sur l'alerte : l'écran ne
        le recalcule jamais, il affiche ce que la dernière écriture de cote a mis
        en base. ``NULL`` se traduit par une ligne absente, pas par zéro.

        `catalogue_exhausted` est le verdict **lu en base** lui aussi : la recherche
        a conclu qu'aucune source ne catalogue l'ouvrage. Il ne remplace quelque
        chose que là où il n'y a ni livre ni série — des livres contrediraient le
        verdict, et la cote qu'ils portent est plus récente que lui.
        """
        books_in_series = books_in_series or []
        # Validation CPU seulement : aucune somme de remplacement n'est publiée.
        if series_total_cents != series_reference_total(books_in_series):
            series_total_cents = None

        # Résolution de repli : un tome non résolu mais une série d'un seul livre.
        if not book and len(books_in_series) == 1 and not (tome_result and tome_result.is_lot):
            book = books_in_series[0]

        if tome_result and tome_result.is_lot:
            # La sélection d'un tome garde ses détails, jamais un écart entre
            # son prix unitaire et l'ensemble de l'annonce.
            card = MarketPriceService._lot_card(books_in_series, tome_result.lot_tomes)
            if book:
                details = MarketPriceService._single_book_card(book, [], latest_snapshot, None)
                card = details + "\n\n" + card.split("\n\n", 1)[-1]
            books_in_series = _match_lot_books(books_in_series, tome_result.lot_tomes or [])
        elif not book and books_in_series:
            card = MarketPriceService._series_card(listing, books_in_series, series_total_cents)
        elif not book and catalogue_exhausted:
            card = MarketPriceService._no_catalogue_card()
        else:
            card = MarketPriceService._single_book_card(
                book, books_in_series, latest_snapshot, series_total_cents
            )

        targets = [book] if book else listable_series_books(books_in_series)
        caches = [b.market_cache for b in targets if b.market_cache]
        cache = max(
            caches,
            key=lambda c: _aware_checked_at(c.last_attempt_at or c.checked_at),
            default=None,
        )
        statuses = {c.collection_status for c in caches}
        status = (
            "failed"
            if statuses == {"failed"}
            else "partial"
            if statuses & {"failed", "partial"}
            else "complete"
            if len(caches) > 1 and any(has_usable_price(c) for c in caches)
            else None
        )
        note = (
            refresh_note
            if refresh_note is not None
            else MarketPriceService._collection_note(cache, status=status)
        )
        if additional_note:
            note = "\n".join(filter(None, (note, additional_note)))
        if note:
            card = card.replace("\n\n", f"\n\n{note}\n\n", 1)
        return card

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
        lines: list[str] = []
        for book in _sort_series_books(listable_series_books(books)):
            is_current = _is_same_book(book, current_book)
            price_cents, _ = reference_price(book.market_cache)
            if price_cents is None and is_current:
                price_cents = fallback_price
            if price_cents is None:
                continue
            entry = f"• {clean_tome_display_title(book.title)} : {format_cents(price_cents)}"
            reason = _reference_reason(book)
            if reason:
                entry += f" ({reason})"
            lines.append(f"• <b>{entry[2:]}</b>" if is_current else entry)
        return lines

    @staticmethod
    def _lot_card(books_in_series: list[Book], lot_tomes: list[int] | None) -> str:
        """Panneau d'un lot : les cotes par tome, et le prix du lot quand il se forme.

        Le prix du lot n'apparaît que si **tous** ses tomes sont cotés : sans
        total complet, il n'y a pas de prix de lot — le panneau affiche les cotes
        par tome, jamais un cumul fictif sur la moitié d'un lot.
        """
        reference = lot_reference(books_in_series, lot_tomes or [])
        target_books = reference.books

        lines = MarketPriceService._header("<b>Cote par tome (plus bas connu) :</b>")
        lines.extend(_reference_detail(book) for book in _sort_series_books(target_books))

        # Un panneau sans cote n'est jamais vide : il dit ce qu'il attend, ou ce
        # qu'une passe a déjà constaté.
        unpriced = _unpriced_tome_lines(target_books)
        if unpriced:
            lines.append(unpriced)

        lot_total = reference.total_cents
        if lot_total is not None:
            lines.append(f"<b>Total du lot :</b> ~{format_cents(lot_total)}")
            kinds = {reference_price(_loaded_market_cache(book))[1] for book in target_books}
            lines.append("Références mixtes, dates distinctes." if len(kinds) > 1
                         else "Somme de références individuelles, dates distinctes.")
        else:
            lines.append(
                f"⚠️ {(reference.reason or 'composition non identifiée').capitalize()} : "
                "comparaison indisponible."
            )

        return "\n".join(lines)

    @staticmethod
    def _series_card(
        listing: Listing | None,
        books_in_series: list[Book],
        series_total_cents: int | None = None,
    ) -> str:
        """Panneau d'une série sans tome résolu : les cotes de tous ses tomes.

        Chaque tome figure — coté avec son prix, muet avec une mention explicite —
        et le total persisté ferme la liste. Un tome absent se lisait comme un
        tome qui n'existe pas.
        """
        vinted_total = _vinted_total_cents(listing) if listing else None
        lines = MarketPriceService._header(
            f"🆕 <b>Vinted :</b> {format_cents(vinted_total) if vinted_total else 'à confirmer'}"
        )
        if listing is None:
            lines = ["📊 <b>Prix du marché</b>\n"]
        else:
            lines.append("Annonce sans tome précis détecté.\n")
        lines.append("<b>Cotes par tome (plus bas connu) :</b>")
        lines.extend(MarketPriceService._series_books_lines(books_in_series, None))
        # Un tome sans cote est une cote en attente, pas une cote absente ; et un
        # tome dont la passe a conclu à l'absence d'offre n'attend plus rien. Les
        # deux comptes remplacent la section vide, qui se lisait comme un panneau mort.
        unpriced = _unpriced_tome_lines(books_in_series)
        if unpriced:
            lines.append(unpriced)
        if series_total_cents is not None:
            lines.append(_series_total_line(series_total_cents, books_in_series))
        return "\n".join(lines)

    @staticmethod
    def _single_book_card(
        book: Book | None,
        books_in_series: list[Book],
        latest_snapshot: Any,
        series_total_cents: int | None = None,
    ) -> str:
        """Panneau d'un tome résolu : cote 1 an, comparatif par boutique, série."""
        cache = book.market_cache if book else None
        reference_cents, is_historical = reference_price(cache)
        if cache is None or (reference_cents is None
                             and cache.lowest_delivered_price_cents is None
                             and cache.historical_low_1y_cents is None):
            return MarketPriceService._no_market_price_card(book)

        snapshots = book.__dict__.get("snapshots")
        partners = _select_secondhand_partners(
            _extract_partners(
                cache, latest_snapshot, snapshots if isinstance(snapshots, list) else None
            )
        )

        lines = ["📊 <b>Prix du marché</b>\n"]
        if book is not None:
            lines.append(
                f"{CATEGORY_EMOJIS[CategorySlug.BD_COMICS]} <b>{html.escape(book.title)}</b>"
            )
        current_price = _positive(cache.lowest_delivered_price_cents)
        if current_price:
            label = (
                "Dernière offre connue"
                if cache.collection_status in ("failed", "empty")
                or MarketPriceService._is_expired(cache)
                else "Meilleure offre actuelle"
            )
            lines.append(f"<b>{label} :</b> {format_cents(current_price)}")
            reason = _reference_reason(book) if book else None
            if reason:
                lines.append(f"⚠️ {reason.capitalize()} : comparaison indisponible.")

        if cache.historical_low_1y_cents is not None:
            lines.append(
                f"Ancien minimum de provenance temporelle non vérifiable : "
                f"{format_cents(cache.historical_low_1y_cents)} (informatif)"
            )
        if is_historical and reference_cents is not None:
            lines.append(
                f"<b>Minimum des relevés sur 365 jours :</b> {format_cents(reference_cents)} "
                f"({_format_annual_date(cache.annual_low_observed_at)})\n{annual_coverage(cache)}"
            )

        if partners:
            if is_historical:
                lines.append(
                    "\n<b>Dernières offres connues :</b>"
                    if cache.collection_status == "failed" or MarketPriceService._is_expired(cache)
                    else "\n<b>Comparatif par boutique :</b>"
                )
            else:
                lines.append("<b>Dernières offres connues (informatives) :</b>"
                             if MarketPriceService._is_expired(cache)
                             else "<b>Comparatif par boutique :</b>")
            for merchant, data in sorted(
                partners.items(), key=lambda item: _positive(item[1].get("total_cents")) or 0
            )[:MAX_PARTNERS_SHOWN]:
                total_cents = _positive(data.get("total_cents")) or 0
                condition = normalize_secondhand_condition(data.get("condition"))
                lines.append(f"• {merchant} : {format_cents(total_cents)} ({condition})")
        elif not is_historical:
            lines.append("(Aucune offre d'occasion comparable en stock chez nos partenaires.)")

        if len(books_in_series) > 1:
            series_lines = MarketPriceService._series_books_lines(
                books_in_series, book, reference_cents
            )
            if len(series_lines) > 1:
                lines.append("\n<b>Autres tomes de la série :</b>")
                lines.extend(series_lines)

        # Le total de série ferme le contexte : un tome isolé ne dit pas à lui
        # seul ce que vaut la collection — sa valeur est en base, dans l'alerte.
        if series_total_cents is not None and len(books_in_series) > 1:
            lines.append(_series_total_line(series_total_cents, books_in_series))

        best_url = MarketPriceService._best_offer_url(cache, partners)
        if best_url:
            lines.append(f'\n<b>Lien :</b> <a href="{best_url}">Consulter l\'offre</a>')

        return "\n".join(lines)

    @staticmethod
    def _collection_note(cache: BookMarketCache | None, *, status: str | None = None) -> str:
        """Une tentative vide ou échouée ne rafraîchit pas un ancien prix."""
        if cache is None:
            return ""
        moment = cache.last_attempt_at or cache.checked_at
        date = f" ({_format_checked_at(moment)})" if moment else ""
        status = status or cache.collection_status
        if status == "failed":
            return f"<i>⚠️ Actualisation impossible{date} ; derniers relevés conservés.</i>"
        if status == "partial":
            return f"<i>⚠️ Actualisation partielle{date}</i>"
        if status == "empty":
            return f"<i>Aucune offre trouvée{date}</i>"
        return f"<b>Actualisation :</b> {_format_checked_at(cache.checked_at)}"

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
    def _no_catalogue_card() -> str:
        """Panneau d'une alerte dont la recherche a conclu qu'aucune source ne la connaît.

        Le panneau annonçait « cote d'occasion en cours de calcul… » pour une série
        que personne ne catalogue : la recherche était terminée, et sa réponse tenait
        en une ligne. La dire est tout ce qui reste à faire (ADR-028, ADR-041).
        """
        return f"📊 <b>Prix du marché</b>\n\n{NO_CATALOGUE_LINE}"

    @staticmethod
    def _no_market_price_card(book: Book | None = None) -> str:
        """Panneau sans cote chiffrée : la même vérité que la ligne d'alerte, datée.

        Le panneau disait déjà l'absence ; il dit désormais **laquelle** — une cote
        qui vient, ou une passe qui n'a rien trouvé — et date ce qu'il a constaté.
        La phrase longue d'avant affirmait une recherche que personne n'avait
        forcément lancée.
        """
        cache = _loaded_market_cache(book) if book else None
        if cache and cache.collection_status in ("failed", "empty"):
            absence = ""
        else:
            absence = cote_absence_line(book) or PENDING_COTE_LINE
            if is_no_offer_line(absence):
                absence = NO_OFFER_LINE
        title = (
            f"{CATEGORY_EMOJIS[CategorySlug.BD_COMICS]} <b>{html.escape(book.title)}</b>\n\n"
            if book
            else ""
        )
        return f"📊 <b>Prix du marché</b>\n\n{title}{absence}"


def annual_coverage(cache: BookMarketCache) -> str:
    """Dates et nombre de relevés ; jamais une promesse de couverture continue."""
    return (
        f"Couverture partielle : {cache.annual_count} relevé(s), "
        f"du {_format_annual_date(cache.annual_first_at)} "
        f"au {_format_annual_date(cache.annual_last_at)}."
    )


def _format_annual_date(moment: datetime | None) -> str:
    """Année et fuseau explicites pour une fenêtre traversant deux années."""
    return _aware_checked_at(moment).astimezone(UTC).strftime("%d/%m/%Y %H:%M:%S UTC")
