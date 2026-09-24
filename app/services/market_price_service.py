"""Service de comparaison instantanée des prix de marché."""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.core.constants import ComparisonStatus
from app.db.models.book import Book
from app.db.models.listing import Listing

if TYPE_CHECKING:
    from app.services.tome_extractor import TomeMatchResult


def format_cents(cents: int) -> str:
    """Formate des centimes en euros."""
    return f"{cents / 100:.2f} €".replace(".", ",")


# Marchands non francophones ou non pertinents à exclure formellement du comparateur
EXCLUDED_MERCHANTS: frozenset[str] = frozenset({
    "amazon (de)",
    "amazon_de",
})


def is_merchant_excluded(merchant_name: str | None) -> bool:
    """Vérifie si un marchand doit être strictement exclu (ex: Amazon DE / Allemagne)."""
    if not merchant_name:
        return False
    name_clean = merchant_name.strip().lower()
    if name_clean in EXCLUDED_MERCHANTS:
        return True
    is_amazon_de = "(de)" in name_clean or "_de" in name_clean or "-de" in name_clean
    return "amazon" in name_clean and is_amazon_de


def is_brand_new_partner(p_data: dict) -> bool:
    """Détecte si une offre partenaire est du Neuf (à exclure de l'occasion)."""
    if not p_data.get("is_used", True):
        return True
    cond = str(p_data.get("condition", "")).strip().lower()
    # "neuf" ou "new", mais PAS "comme neuf" / "like new"
    return bool(
        (cond in ("neuf", "new") or cond.startswith("neuf ") or cond.endswith(" neuf"))
        and ("comme" not in cond and "like" not in cond)
    )


def is_very_good_or_better(condition_str: str | None) -> bool:
    """Vérifie si l'état d'un exemplaire est au moins 'Très bon' (Très bon ou Comme neuf).

    Exclut strictement : Bon, Correct, Acceptable, Moyen, etc.
    """
    if not condition_str:
        return False
    c = condition_str.strip().lower()

    # Exclusions explicites d'états inférieurs
    inferior_keywords = [
        "correct",
        "acceptable",
        "moyen",
        "passable",
        "usag",
        "usé",
        "defraichi",
        "défraîchi",
    ]
    if any(kw in c for kw in inferior_keywords):
        return False

    # Autoriser uniquement 'comme neuf' / 'like new' ou 'très bon' / 'very good' / 'tb'
    return bool(
        re.search(
            r"\b(comme\s*neuf|like\s*new|near\s*mint|parfait|neuf\s*sous\s*blister|tr[eè]s\s*bon|very\s*good|tb\+?)\b",
            c,
        )
    )


def normalize_secondhand_condition(cond: str | None) -> str:
    """Nettoie le libellé d'état pour affichage compact (ex: 'Tres bon', 'Comme neuf')."""
    if not cond:
        return "Très bon"
    c = cond.strip().lower()
    if "comme neuf" in c or "like new" in c or "near mint" in c:
        return "Comme neuf"
    return "Très bon"


def clean_tome_display_title(title: str, include_subtitle: bool = False) -> str:
    """Nettoie le titre du tome pour affichage.

    Si include_subtitle est True, conserve le sous-titre entre parenthèses s'il existe.
    """
    if not title:
        return ""
    cleaned = title.strip()

    # 1. Détection explicite du Tome / Volume / Intégrale
    tome_match = re.search(
        r"\b(Tome\s+\d+|Volume\s+\d+|Intégrale\b[^\-:\(]*)",
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
        # 1. Cas d'un livre précis résolu (tome unique ou one-shot)
        if book and book.market_cache:
            cache = book.market_cache

            # Statut UNAVAILABLE sans historique bas certifié
            if (
                cache.comparison_status == ComparisonStatus.UNAVAILABLE
                and cache.historical_low_1y_cents is None
            ):
                return None

            # Cache expiré sans historique bas certifié
            now = datetime.now(UTC)
            exp = (
                cache.expires_at.replace(tzinfo=UTC)
                if (cache.expires_at and cache.expires_at.tzinfo is None)
                else cache.expires_at
            )
            if exp and exp < now and cache.historical_low_1y_cents is None:
                return None

            ref_price_cents = cache.historical_low_1y_cents or cache.lowest_delivered_price_cents

            if ref_price_cents is not None and ref_price_cents > 0:
                lines: list[str] = []
                tome_suffix = (
                    f" (T{tome_result.tome_number})"
                    if (tome_result and tome_result.tome_number)
                    else ""
                )

                if cache.historical_low_1y_cents is not None and cache.historical_low_1y_cents > 0:
                    hist_str = format_cents(cache.historical_low_1y_cents)
                    lines.append(f"📉 Prix bas constaté{tome_suffix} : {hist_str}")
                else:
                    prov_name = (
                        cache.best_provider_name.replace("_", " ").title()
                        if cache.best_provider_name
                        else ""
                    )
                    prov_suffix = f" ({prov_name})" if prov_name else ""
                    market_str = format_cents(ref_price_cents)
                    lines.append(f"📊 Prix du marché{tome_suffix} : {market_str}{prov_suffix}")

                # Ligne 2 : Écart par rapport au prix de référence
                vinted_total = listing.estimated_total_cents
                if vinted_total is None and listing.item_price_cents:
                    from app.services.price_calculator import calculate_estimated_total

                    vinted_total = calculate_estimated_total(
                        listing.item_price_cents, fallback_defaults=True
                    )

                if vinted_total is not None:
                    diff_cents = vinted_total - ref_price_cents
                    diff_euros = format_cents(abs(diff_cents))

                    if diff_cents <= 0:
                        icon = "✅"
                        diff_str = f"-{diff_euros}" if diff_cents < 0 else "0,00 €"
                    else:
                        icon = "⛔️"
                        diff_str = f"+{diff_euros}"

                    lines.append(f"{icon} Écart : <b>{diff_str}</b>")

                return "\n".join(lines)

        # 2. Cas d'un lot avec tomes identifiés (ex: [1, 2, 3])
        if tome_result and tome_result.is_lot and tome_result.lot_tomes and books_in_series:
            matching = [
                b
                for b in books_in_series
                if any(
                    re.search(rf"\b(?:tome|t|vol)\s*\.?\s*{t}\b", b.title, re.IGNORECASE)
                    for t in tome_result.lot_tomes
                )
            ]
            target_books = matching if matching else books_in_series
            lot_ref_prices = [
                (
                    b.market_cache.historical_low_1y_cents
                    or b.market_cache.lowest_delivered_price_cents
                )
                for b in target_books
                if b.market_cache
                and (
                    b.market_cache.historical_low_1y_cents is not None
                    or b.market_cache.lowest_delivered_price_cents is not None
                )
            ]
            if lot_ref_prices:
                cumulated = sum(p for p in lot_ref_prices if p)
                all_have_hist = all(
                    b.market_cache and b.market_cache.historical_low_1y_cents is not None
                    for b in target_books
                )
                prefix = "📉 Prix bas constaté" if all_have_hist else "📊 Prix du marché"
                lot_lines: list[str] = [
                    f"{prefix} (Lot) : ~{format_cents(cumulated)}"
                ]
                vinted_total = listing.estimated_total_cents
                if vinted_total is None and listing.item_price_cents:
                    from app.services.price_calculator import calculate_estimated_total

                    vinted_total = calculate_estimated_total(
                        listing.item_price_cents, fallback_defaults=True
                    )

                if vinted_total is not None:
                    diff_cents = vinted_total - cumulated
                    diff_euros = format_cents(abs(diff_cents))
                    icon = "✅" if diff_cents <= 0 else "⛔️"
                    diff_str = (
                        f"-{diff_euros}"
                        if diff_cents < 0
                        else (f"+{diff_euros}" if diff_cents > 0 else "0,00 €")
                    )
                    lot_lines.append(f"{icon} Écart : <b>{diff_str}</b>")
                return "\n".join(lot_lines)

        # 3. Non-reconnaissance du tome / tomes dans une série (aucune information suffisante)
        if books_in_series and len(books_in_series) > 1:
            return "📉 Prix bas constaté :  —\n⚖️ Écart :  —"

        return None

    @staticmethod
    def format_market_details_card(
        listing: Listing,
        book: Book | None,
        books_in_series: list[Book] | None = None,
        tome_result: "TomeMatchResult | None" = None,
    ) -> str:
        """Génère le texte complet du panneau callback 'Prix du marché'."""
        # Résolution de repli : si book est None mais qu'un seul livre est présent dans la série
        if (
            not book
            and books_in_series
            and len(books_in_series) == 1
            and not (tome_result and tome_result.is_lot)
        ):
            book = books_in_series[0]

        # A. Cas d'un lot ou d'une série sans tome unique
        if (
            not book
            and books_in_series
            and (len(books_in_series) > 1 or (tome_result and tome_result.is_lot))
        ):
            vinted_str = (
                format_cents(listing.estimated_total_cents)
                if listing.estimated_total_cents
                else "à confirmer"
            )

            # Date de dernière mise à jour parmi les tomes de la série
            latest_checked = max(
                (
                    b.market_cache.checked_at
                    for b in books_in_series
                    if b.market_cache and b.market_cache.checked_at
                ),
                default=None,
            )
            checked_time = "récemment"
            if latest_checked:
                try:
                    paris_tz = ZoneInfo("Europe/Paris")
                    dt = (
                        latest_checked
                        if latest_checked.tzinfo
                        else latest_checked.replace(tzinfo=UTC)
                    )
                    checked_time = dt.astimezone(paris_tz).strftime("%d/%m à %H:%M")
                except Exception:
                    checked_time = latest_checked.strftime("%d/%m à %H:%M")

            if tome_result and tome_result.is_lot:
                target_series_books = books_in_series
                if tome_result.lot_tomes:
                    matching = [
                        b
                        for b in books_in_series
                        if any(
                            re.search(rf"\b(?:tome|t|vol)\s*\.?\s*{t}\b", b.title, re.IGNORECASE)
                            for t in tome_result.lot_tomes
                        )
                    ]
                    if matching:
                        target_series_books = matching

                sorted_lot_books = sorted(
                    target_series_books,
                    key=lambda b: (
                        extract_tome_number_from_title(b.title) is None,
                        extract_tome_number_from_title(b.title) or 0,
                        b.title or "",
                    ),
                )

                lines = [
                    "📊 <b>Prix du marché</b>\n",
                    "<b>Cote par tome (Min sur 1 an) :</b>",
                ]
                for b in sorted_lot_books:
                    ref = (
                        (
                            b.market_cache.historical_low_1y_cents
                            or b.market_cache.lowest_delivered_price_cents
                        )
                        if b.market_cache
                        else None
                    )
                    if ref:
                        t_name = clean_tome_display_title(b.title)
                        lines.append(f"• {t_name} : {format_cents(ref)}")
                lines.append(f"\nDerniere MAJ : {checked_time}")
                return "\n".join(lines)

            else:
                lines = [
                    "📊 <b>Prix du marché</b>\n",
                    f"🆕 Vinted : {vinted_str}",
                    "<i>Annonce sans tome précis détecté.</i>\n",
                    "<b>Cotes par tome (Min sur 1 an) :</b>",
                ]
                has_num = any(
                    extract_tome_number_from_title(b.title) is not None for b in books_in_series
                )
                sorted_fallback_books = sorted(
                    books_in_series,
                    key=lambda b: (
                        extract_tome_number_from_title(b.title) is None,
                        extract_tome_number_from_title(b.title) or 0,
                        b.title or "",
                    ),
                )
                for b in sorted_fallback_books:
                    if has_num and extract_tome_number_from_title(b.title) is None:
                        continue
                    ref = (
                        (
                            b.market_cache.historical_low_1y_cents
                            or b.market_cache.lowest_delivered_price_cents
                        )
                        if b.market_cache
                        else None
                    )
                    if ref:
                        t_name = clean_tome_display_title(b.title)
                        lines.append(f"• {t_name} : {format_cents(ref)}")
                lines.append(f"\nDerniere MAJ : {checked_time}")
                return "\n".join(lines)

        # B. Cas normal : livre unique ou tome résolu
        if not book or not book.market_cache:
            return (
                "📊 <b>Prix du marché</b>\n\n"
                "Aucune offre d'occasion comparable n'a pu être trouvée "
                "pour ce titre sur les plateformes partenaires."
            )

        cache = book.market_cache
        lowest_delivered = cache.lowest_delivered_price_cents
        ref_price = cache.historical_low_1y_cents or lowest_delivered
        if ref_price is None:
            return (
                "📊 <b>Prix du marché</b>\n\n"
                "Aucune offre d'occasion comparable n'a pu être trouvée "
                "pour ce titre sur les plateformes partenaires."
            )

        # Format de date locale Europe/Paris
        checked_time = "récemment"
        if cache.checked_at:
            try:
                paris_tz = ZoneInfo("Europe/Paris")
                dt = (
                    cache.checked_at
                    if cache.checked_at.tzinfo
                    else cache.checked_at.replace(tzinfo=UTC)
                )
                checked_time = dt.astimezone(paris_tz).strftime("%d/%m à %H:%M")
            except Exception:
                checked_time = cache.checked_at.strftime("%d/%m à %H:%M")

        if listing.estimated_total_cents is not None:
            vinted_str = format_cents(listing.estimated_total_cents)
        else:
            vinted_str = "à confirmer"

        # Détail des partenaires si disponible
        partners = None
        if book:
            snapshots = book.__dict__.get("snapshots")
            if snapshots and isinstance(snapshots, list):
                for snap in sorted(
                    snapshots, key=lambda s: s.checked_at or datetime.min, reverse=True
                ):
                    if (
                        snap.raw_payload
                        and isinstance(snap.raw_payload, dict)
                        and "partners" in snap.raw_payload
                    ):
                        partners = snap.raw_payload["partners"]
                        break

        if not partners:
            raw_payload = getattr(cache, "raw_payload", None) or {}
            partners = raw_payload.get("partners") if isinstance(raw_payload, dict) else None

        # Filtrage STRICT : occasion en Très bon ou Comme neuf (exclusion Amazon DE, etc.)
        valid_used_partners: dict[str, dict] = {}
        if partners and isinstance(partners, dict):
            for p_name, p_data in partners.items():
                if is_merchant_excluded(p_name) or is_brand_new_partner(p_data):
                    continue
                if (
                    is_very_good_or_better(p_data.get("condition"))
                    and p_data.get("total_cents", 0) > 0
                ):
                    valid_used_partners[p_name] = p_data

        # lowest_delivered sur le minimum réel parmi les partenaires filtrés
        if valid_used_partners:
            lowest_delivered = min(p["total_cents"] for p in valid_used_partners.values())

        # Priorité au prix actuel en direct, sinon repli sur l'historique 1 an
        ref_price = lowest_delivered or cache.historical_low_1y_cents

        lines = [
            "📊 <b>Prix du marché</b>\n",
        ]

        if valid_used_partners:
            lines.append("<b>Comparatif par boutique :</b>")
            sorted_partners = sorted(
                valid_used_partners.items(),
                key=lambda item: item[1].get("total_cents", 999999),
            )[:5]
            for p_name, p_data in sorted_partners:
                p_cents = p_data.get("total_cents", 0)
                p_cond = normalize_secondhand_condition(p_data.get("condition"))
                lines.append(f"• {p_name} : {format_cents(p_cents)} ({p_cond})")
        elif cache.historical_low_1y_cents:
            lines.append(f"📉 Prix bas constaté : {format_cents(cache.historical_low_1y_cents)}")
            lines.append("<i>(Cote d'occasion basée sur le prix bas constaté 1 an)</i>")

        if books_in_series and len(books_in_series) > 1:
            has_numbered_tomes = any(
                extract_tome_number_from_title(b.title) is not None for b in books_in_series
            )
            sorted_books = sorted(
                books_in_series,
                key=lambda b: (
                    extract_tome_number_from_title(b.title) is None,
                    extract_tome_number_from_title(b.title) or 0,
                    b.title or "",
                ),
            )

            series_lines: list[str] = []
            for b in sorted_books:
                t_num = extract_tome_number_from_title(b.title)
                if has_numbered_tomes and t_num is None:
                    continue

                is_current = bool(
                    book
                    and (
                        b is book
                        or (b.id is not None and book.id is not None and b.id == book.id)
                        or (b.title and book.title and b.title.lower() == book.title.lower())
                    )
                )
                b_cache = b.market_cache if b.market_cache else (cache if is_current else None)
                b_price = (
                    (b_cache.historical_low_1y_cents or b_cache.lowest_delivered_price_cents)
                    if b_cache
                    else None
                )
                if is_current and not b_price:
                    b_price = ref_price

                if not b_price:
                    continue

                t_name = clean_tome_display_title(b.title)
                price_str = format_cents(b_price)

                if is_current:
                    series_lines.append(f"• <b>{t_name} : {price_str}</b>")
                else:
                    series_lines.append(f"• {t_name} : {price_str}")

            if series_lines and len(series_lines) > 1:
                lines.append("\n<b>Autres tomes de la série :</b>")
                lines.extend(series_lines)

        best_url = cache.best_source_url
        if valid_used_partners:
            best_partner = sorted(
                valid_used_partners.items(),
                key=lambda item: item[1].get("total_cents", 999999),
            )[0][1]
            if best_partner.get("url"):
                best_url = best_partner.get("url")

        if best_url:
            lines.append(f'\nLien : <a href="{best_url}">Consulter l\'offre</a>')

        lines.append(f"\nDerniere MAJ : {checked_time}")

        return "\n".join(lines)
