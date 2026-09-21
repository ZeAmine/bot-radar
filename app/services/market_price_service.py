"""Service de comparaison instantanée des prix de marché."""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

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


def clean_tome_display_title(title: str, include_subtitle: bool = False) -> str:
    """Nettoie le titre du tome pour affichage.
    
    Si include_subtitle est True, conserve le sous-titre entre parenthèses s'il existe.
    """
    if include_subtitle:
        if "—" in title:
            part = title.split("—")[-1].strip()
            if part:
                return part
        if " - " in title:
            part = title.split(" - ")[-1].strip()
            if part:
                return part
        return title.strip()

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
        """Génère la ligne de comparaison épurée (Prix bas constaté + Écart) pour la légende Telegram."""
        # 1. Cas d'un livre précis résolu (tome unique ou one-shot)
        if book and book.market_cache:
            cache = book.market_cache
            has_hist_low = cache.historical_low_1y_cents is not None

            if has_hist_low and cache.historical_low_1y_cents is not None:
                lines: list[str] = []
                tome_suffix = (
                    f" (T{tome_result.tome_number})"
                    if (tome_result and tome_result.tome_number)
                    else ""
                )

                # Ligne 1 : Prix bas constaté sur 1 an
                hist_str = format_cents(cache.historical_low_1y_cents)
                lines.append(f"📉 Prix bas constaté{tome_suffix} : {hist_str}")

                # Ligne 2 : Écart par rapport au prix bas constaté
                if listing.estimated_total_cents is not None:
                    vinted_total = listing.estimated_total_cents
                    diff_cents = vinted_total - cache.historical_low_1y_cents
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
            hist_prices = [
                b.market_cache.historical_low_1y_cents
                for b in target_books
                if b.market_cache and b.market_cache.historical_low_1y_cents is not None
            ]
            if hist_prices:
                hist_cumulated = sum(hist_prices)
                tomes_label = f" [{', '.join(str(t) for t in sorted(tome_result.lot_tomes))}]"
                lot_lines: list[str] = [
                    f"📉 Prix bas constaté{tomes_label} : ~{format_cents(hist_cumulated)}"
                ]
                if listing.estimated_total_cents is not None:
                    diff_cents = listing.estimated_total_cents - hist_cumulated
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

                lines = [
                    "📊 <b>Prix du marché</b>\n",
                    "<b>Cote par tome (Min sur 1 an) :</b>",
                ]
                for b in target_series_books:
                    t_name = clean_tome_display_title(b.title, include_subtitle=True)
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
                    t_name = clean_tome_display_title(b.title, include_subtitle=True)
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

        t_name = clean_tome_display_title(book.title, include_subtitle=True) if book and book.title else ""
        header = f"📊 <b>Prix du marché</b> — {t_name}\n" if t_name else "📊 <b>Prix du marché</b>\n"
        lines = [
            header,
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
