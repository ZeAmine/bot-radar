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
    if "amazon" in name_clean and ("(de)" in name_clean or "_de" in name_clean or "-de" in name_clean):
        return True
    return False


def is_brand_new_partner(p_data: dict) -> bool:
    """Détecte si une offre partenaire est du Neuf (à exclure rigoureusement du comparatif d'occasion)."""
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


def clean_tome_display_title(title: str) -> str:
    """Supprime les sous-titres et noms superflus du tome (ex: parenthèses).

    Exemples :
    - 'Blast — Tome 1 (Grasse Carcasse)' -> 'Tome 1'
    - 'RIP — Tome 2 (Maurice — Les mouches ne mentent pas)' -> 'Tome 2'
    - 'Blacksad — Tome 6 (Alors, tout tombe (Première partie))' -> 'Tome 6'
    - 'Blacksad — Intégrale' -> 'Intégrale'
    """
    cleaned = re.sub(r"\s*\([^)]*\)", "", title).strip()
    while "(" in cleaned and ")" in cleaned:
        cleaned = re.sub(r"\s*\([^)]*\)", "", cleaned).strip()

    tome_match = re.search(
        r"\b(Tome\s+\d+|Volume\s+\d+|Intégrale\b[^\-:]*)", cleaned, re.IGNORECASE
    )
    if tome_match:
        return tome_match.group(0).strip().capitalize()

    if "—" in cleaned:
        part = cleaned.split("—")[-1].strip()
        if part:
            return part
    if " - " in cleaned:
        part = cleaned.split(" - ")[-1].strip()
        if part:
            return part

    return cleaned


class MarketPriceService:
    """Service de consultation du cache de marché et de calcul d'écarts."""

    @staticmethod
    def get_comparison_summary(
        listing: Listing,
        book: Book | None,
        books_in_series: list[Book] | None = None,
        tome_result: "TomeMatchResult | None" = None,
    ) -> str | None:
        """Génère la ligne de comparaison de prix pour la légende Telegram (zéro requête réseau)."""
        now = datetime.now(UTC)

        # 1. Cas d'un livre précis résolu (tome unique ou one-shot)
        if book and book.market_cache:
            cache = book.market_cache
            is_live_valid = (
                cache.comparison_status == ComparisonStatus.AVAILABLE
                and cache.lowest_delivered_price_cents is not None
                and (cache.expires_at is None or cache.expires_at >= now)
            )
            has_hist_low = cache.historical_low_1y_cents is not None

            if is_live_valid or has_hist_low:
                lines: list[str] = []
                tome_suffix = (
                    f" (T{tome_result.tome_number})"
                    if (tome_result and tome_result.tome_number)
                    else ""
                )

                # Ligne 1 : Prix du marché actuel (Très bon état +)
                if is_live_valid and cache.lowest_delivered_price_cents is not None:
                    live_str = format_cents(cache.lowest_delivered_price_cents)
                    p_name = cache.best_provider_name
                    merchant_str = (
                        f" ({p_name})" if p_name and p_name.lower() != "chasse_aux_livres" else ""
                    )
                    lines.append(f"📊 Prix du marché{tome_suffix} : {live_str}{merchant_str}")
                else:
                    lines.append(f"📊 Prix du marché{tome_suffix} : Indisponible (occasion)")

                # Ligne 2 : Plus bas constaté sur 1 an
                if has_hist_low and cache.historical_low_1y_cents is not None:
                    hist_str = format_cents(cache.historical_low_1y_cents)
                    lines.append(f"🎯 Plus bas constaté{tome_suffix} : {hist_str}")

                # Ligne 3 : Écart par rapport au marché (ou plus bas constaté si marché absent)
                ref_price = (
                    cache.lowest_delivered_price_cents
                    if (is_live_valid and cache.lowest_delivered_price_cents is not None)
                    else cache.historical_low_1y_cents
                )

                if listing.estimated_total_cents is not None and ref_price is not None:
                    vinted_total = listing.estimated_total_cents
                    diff_cents = vinted_total - ref_price
                    diff_euros = format_cents(abs(diff_cents))

                    if diff_cents <= 0:
                        icon = "✅"
                        diff_str = f"-{diff_euros}" if diff_cents < 0 else "0,00 €"
                    else:
                        icon = "⛔️"
                        diff_str = f"+{diff_euros}"

                    lines.append(f"{icon} Écart : <b>{diff_str}</b>")

                if lines:
                    return "\n".join(lines)

        # 2. Cas d'un lot détecté
        if tome_result and tome_result.is_lot and books_in_series:
            target_books = books_in_series
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
                    target_books = matching

            ref_prices = [
                (
                    b.market_cache.lowest_delivered_price_cents
                    if (
                        b.market_cache.comparison_status == ComparisonStatus.AVAILABLE
                        and b.market_cache.lowest_delivered_price_cents is not None
                        and (b.market_cache.expires_at is None or b.market_cache.expires_at >= now)
                    )
                    else b.market_cache.historical_low_1y_cents
                )
                for b in target_books
                if b.market_cache
                and (
                    (
                        b.market_cache.comparison_status == ComparisonStatus.AVAILABLE
                        and b.market_cache.lowest_delivered_price_cents is not None
                    )
                    or b.market_cache.historical_low_1y_cents is not None
                )
            ]

            live_prices = [
                b.market_cache.lowest_delivered_price_cents
                for b in target_books
                if b.market_cache
                and b.market_cache.comparison_status == ComparisonStatus.AVAILABLE
                and b.market_cache.lowest_delivered_price_cents is not None
                and (b.market_cache.expires_at is None or b.market_cache.expires_at >= now)
            ]

            hist_prices = [
                b.market_cache.historical_low_1y_cents
                for b in target_books
                if b.market_cache and b.market_cache.historical_low_1y_cents is not None
            ]

            if ref_prices:
                cumulated_market = sum(ref_prices)
                count_str = f"{len(ref_prices)} tomes"
                tomes_label = (
                    f" (Lot T{', T'.join(str(t) for t in sorted(tome_result.lot_tomes))})"
                    if tome_result.lot_tomes
                    else f" (Lot {count_str})"
                )

                lot_lines: list[str] = []

                if live_prices:
                    live_cumulated = sum(live_prices)
                    lot_lines.append(
                        f"📊 Prix du marché{tomes_label} : ~{format_cents(live_cumulated)}"
                    )
                else:
                    lot_lines.append(f"📊 Prix du marché{tomes_label} : Indisponible (occasion)")

                if hist_prices:
                    hist_cumulated = sum(hist_prices)
                    lot_lines.append(
                        f"🎯 Plus bas constaté (Lot) : ~{format_cents(hist_cumulated)}"
                    )

                if listing.estimated_total_cents is not None:
                    vinted_total = listing.estimated_total_cents
                    diff_cents = vinted_total - cumulated_market
                    diff_euros = format_cents(abs(diff_cents))
                    if diff_cents <= 0:
                        icon = "✅"
                        diff_str = f"-{diff_euros}" if diff_cents < 0 else "0,00 €"
                    else:
                        icon = "⛔️"
                        diff_str = f"+{diff_euros}"
                    lot_lines.append(f"{icon} Écart : <b>{diff_str}</b>")

                return "\n".join(lot_lines)
            return "📦 Lot détecté (plusieurs tomes)"

        # 3. Cas d'une série multi-tomes sans tome identifié
        if books_in_series and len(books_in_series) > 1:
            series_lines: list[str] = []
            live_prices: list[int] = []
            hist_prices: list[int] = []
            ref_prices: list[int] = []

            for b in books_in_series:
                if not b.market_cache:
                    continue
                cache = b.market_cache
                is_live_valid = (
                    cache.comparison_status == ComparisonStatus.AVAILABLE
                    and cache.lowest_delivered_price_cents is not None
                    and (cache.expires_at is None or cache.expires_at >= now)
                )
                if is_live_valid and cache.lowest_delivered_price_cents is not None:
                    live_prices.append(cache.lowest_delivered_price_cents)
                    ref_prices.append(cache.lowest_delivered_price_cents)
                elif cache.historical_low_1y_cents is not None:
                    ref_prices.append(cache.historical_low_1y_cents)

                if cache.historical_low_1y_cents is not None:
                    hist_prices.append(cache.historical_low_1y_cents)

            market_candidates = live_prices if live_prices else ref_prices
            if market_candidates:
                p_min = min(market_candidates)
                p_max = max(market_candidates)
                if p_min == p_max:
                    series_lines.append(f"📊 Prix du marché : ~{format_cents(p_min)} (selon tome)")
                else:
                    p_min_str = format_cents(p_min)
                    p_max_str = format_cents(p_max)
                    series_lines.append(
                        f"📊 Prix du marché : {p_min_str} - {p_max_str} (selon tome)"
                    )

            if hist_prices:
                h_min = min(hist_prices)
                h_max = max(hist_prices)
                if h_min == h_max:
                    series_lines.append(f"🎯 Plus bas constaté : {format_cents(h_min)}")
                else:
                    h_min_str = format_cents(h_min)
                    h_max_str = format_cents(h_max)
                    series_lines.append(f"🎯 Plus bas constaté : {h_min_str} - {h_max_str}")

            target_diff_prices = hist_prices if hist_prices else ref_prices
            if listing.estimated_total_cents is not None and target_diff_prices:
                vinted_total = listing.estimated_total_cents
                diff_best = vinted_total - max(target_diff_prices)
                diff_worst = vinted_total - min(target_diff_prices)

                if diff_best == diff_worst:
                    diff_cents = diff_best
                    diff_euros = format_cents(abs(diff_cents))
                    if diff_cents <= 0:
                        icon = "✅"
                        diff_str = f"-{diff_euros}" if diff_cents < 0 else "0,00 €"
                    else:
                        icon = "⛔️"
                        diff_str = f"+{diff_euros}"
                    series_lines.append(f"{icon} Écart : <b>{diff_str}</b>")
                else:

                    def _fmt_diff(cents: int) -> str:
                        if cents < 0:
                            return f"-{format_cents(abs(cents))}"
                        elif cents > 0:
                            return f"+{format_cents(cents)}"
                        return "0,00 €"

                    if diff_worst <= 0:
                        icon = "✅"
                        diff_str = f"{_fmt_diff(diff_worst)} à {_fmt_diff(diff_best)}"
                    elif diff_best >= 0:
                        icon = "⛔️"
                        diff_str = f"{_fmt_diff(diff_worst)} à {_fmt_diff(diff_best)}"
                    else:
                        icon = "⚖️"
                        diff_str = f"{_fmt_diff(diff_worst)} à {_fmt_diff(diff_best)}"

                    series_lines.append(f"{icon} Écart : <b>{diff_str}</b>")

            if series_lines:
                return "\n".join(series_lines)

        return "📊 Prix du marché : indisponible"

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

                lines = [
                    "📊 <b>Prix du marché</b>\n",
                    f"🆕 Vinted : {vinted_str}",
                ]
                valid_books = [
                    b
                    for b in target_series_books
                    if b.market_cache
                    and (
                        b.market_cache.historical_low_1y_cents is not None
                        or b.market_cache.lowest_delivered_price_cents is not None
                    )
                ]
                if valid_books:
                    cumulated = sum(
                        (
                            b.market_cache.historical_low_1y_cents
                            or b.market_cache.lowest_delivered_price_cents
                        )
                        for b in valid_books
                    )
                    tomes_desc = (
                        f"T{', T'.join(str(t) for t in sorted(tome_result.lot_tomes))}"
                        if tome_result.lot_tomes
                        else f"{len(valid_books)} tomes"
                    )
                    lines.append(f"💵 Lot ({tomes_desc}) : ~{format_cents(cumulated)}")
                    if listing.estimated_total_cents:
                        diff = listing.estimated_total_cents - cumulated
                        if diff < 0:
                            lines.append(f"✅ Écart : -{format_cents(abs(diff))}")
                        elif diff > 0:
                            lines.append(f"⛔️ Écart : +{format_cents(abs(diff))}")
                        else:
                            lines.append("✅ Écart : 0,00 €")
                lines.append("\n<b>Cote par tome (Min sur 1 an) :</b>")
                for b in target_series_books:
                    t_name = clean_tome_display_title(b.title)
                    ref = (
                        (
                            b.market_cache.historical_low_1y_cents
                            or b.market_cache.lowest_delivered_price_cents
                        )
                        if b.market_cache
                        else None
                    )
                    if ref:
                        lines.append(f"• {t_name} : {format_cents(ref)}")
                    else:
                        lines.append(f"• {t_name} : Cote indisponible")
                lines.append(f"\nDerniere MAJ : {checked_time}")
                return "\n".join(lines)

            else:
                lines = [
                    "📊 <b>Prix du marché</b>\n",
                    f"🆕 Vinted : {vinted_str}",
                    "<i>Annonce sans tome précis détecté.</i>\n",
                    "<b>Cotes par tome (Min sur 1 an) :</b>",
                ]
                for b in books_in_series:
                    t_name = clean_tome_display_title(b.title)
                    ref = (
                        (
                            b.market_cache.historical_low_1y_cents
                            or b.market_cache.lowest_delivered_price_cents
                        )
                        if b.market_cache
                        else None
                    )
                    if ref:
                        lines.append(f"• {t_name} : {format_cents(ref)}")
                    else:
                        lines.append(f"• {t_name} : Cote indisponible")
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

        # Filtrage STRICT : uniquement l'occasion en état Très bon ou Comme neuf (exclusion Bon, Correct, Amazon DE, etc.)
        valid_used_partners: dict[str, dict] = {}
        if partners and isinstance(partners, dict):
            for p_name, p_data in partners.items():
                if (
                    isinstance(p_data, dict)
                    and not is_merchant_excluded(p_name)
                    and not is_merchant_excluded(p_data.get("merchant"))
                    and not is_brand_new_partner(p_data)
                    and is_very_good_or_better(p_data.get("condition"))
                    and p_data.get("total_cents", 0) > 0
                ):
                    valid_used_partners[p_name] = p_data

        # Aligner lowest_delivered sur le minimum réel parmi les partenaires filtrés (Très bon / Comme neuf)
        if valid_used_partners:
            lowest_delivered = min(p["total_cents"] for p in valid_used_partners.values())

        # Priorité au prix du marché actuel en direct si disponible, sinon repli sur l'historique 1 an
        ref_price = lowest_delivered or cache.historical_low_1y_cents

        lines = [
            "📊 <b>Prix du marché</b>\n",
            f"🆕 Vinted : {vinted_str}",
            f"💵 Offres : {format_cents(ref_price)}",
        ]

        if listing.estimated_total_cents is not None and ref_price:
            diff_cents = listing.estimated_total_cents - ref_price
            diff_euros = format_cents(abs(diff_cents))
            if diff_cents > 0:
                lines.append(f"⛔️ Écart : +{diff_euros}")
            elif diff_cents < 0:
                lines.append(f"✅ Écart : -{diff_euros}")
            else:
                lines.append("✅ Écart : 0,00 €")

        if valid_used_partners:
            lines.append("\n<b>Comparatif par boutique :</b>")
            sorted_partners = sorted(
                valid_used_partners.items(),
                key=lambda item: item[1].get("total_cents", 999999),
            )[:5]
            for p_name, p_data in sorted_partners:
                p_cents = p_data.get("total_cents", 0)
                p_cond = normalize_secondhand_condition(p_data.get("condition"))
                lines.append(f"• {p_name} : {format_cents(p_cents)} ({p_cond})")
        elif cache.historical_low_1y_cents:
            lines.append("\n<i>(Cote d'occasion basée sur le plus bas historique 1 an)</i>")

        if books_in_series and len(books_in_series) > 1:
            other_books = [b for b in books_in_series if b.id != book.id]
            if other_books:
                lines.append("\n<b>Autres tomes de la série :</b>")
                for ob in other_books:
                    t_name = clean_tome_display_title(ob.title)
                    ob_price = (
                        (
                            ob.market_cache.historical_low_1y_cents
                            or ob.market_cache.lowest_delivered_price_cents
                        )
                        if ob.market_cache
                        else None
                    )
                    if ob_price:
                        lines.append(f"• {t_name} : {format_cents(ob_price)}")
                    else:
                        lines.append(f"• {t_name} : Cote indisponible")

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
