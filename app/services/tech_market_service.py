"""Service d'estimation de la cote revente et de calcul de marge nette pour Tech & Vêtements."""

import re
from typing import NamedTuple

from app.bot.formatter import format_cents_to_euros
from app.db.models.alert import Alert
from app.db.models.listing import Listing


class TechBenchmark(NamedTuple):
    product_name: str
    resale_target_cents: int
    keywords: list[str]


# Référentiel de cote marchande moyenne constatée sur eBay (ventes réussies) et Leboncoin
TECH_BENCHMARKS: list[TechBenchmark] = [
    # Jeux Switch Tier 1 & Best-Sellers
    TechBenchmark("Mario Kart 8 Deluxe", 3800, ["mario kart 8", "mk8"]),
    TechBenchmark("Super Smash Bros Ultimate", 4000, ["smash bros", "smash bros ultimate"]),
    TechBenchmark("Zelda Breath of the Wild", 4000, ["breath of the wild", "botw"]),
    TechBenchmark("Zelda Tears of the Kingdom", 4200, ["tears of the kingdom", "totk"]),
    TechBenchmark("Super Mario Odyssey", 3600, ["mario odyssey"]),
    TechBenchmark("Animal Crossing New Horizons", 3500, ["animal crossing", "new horizons"]),
    TechBenchmark("Luigi's Mansion 3", 3500, ["luigi mansion 3", "luigi's mansion 3"]),
    TechBenchmark("Super Mario Bros Wonder", 3800, ["mario wonder", "mario bros wonder"]),
    TechBenchmark("Pokémon Écarlate / Violet", 3500, ["pokemon ecarlate", "pokemon violet", "pokémon"]),
    TechBenchmark("Pokémon Légendes Arceus", 3600, ["arceus", "pokemon arceus", "pokémon arceus"]),
    TechBenchmark("Super Mario Party Jamboree", 4200, ["party jamboree", "mario party jamboree"]),
    # Calculatrices
    TechBenchmark("TI-83 Premium CE", 5500, ["ti-83", "ti 83", "83 premium", "ti83"]),
    TechBenchmark("TI-84 Plus CE", 6000, ["ti-84", "ti 84", "ti84"]),
    TechBenchmark("NumWorks", 6500, ["numworks", "num works"]),
    TechBenchmark("TI-Nspire CX II", 8500, ["nspire", "ti-nspire"]),
    # Consoles portables & High-Tech
    TechBenchmark("New Nintendo 3DS XL", 15000, ["new 3ds xl", "new nintendo 3ds xl"]),
    TechBenchmark("Nintendo 3DS XL", 11000, ["3ds xl"]),
    TechBenchmark("Nintendo 2DS XL", 10000, ["2ds xl", "new 2ds xl"]),
    TechBenchmark("Nintendo Switch Oled", 23000, ["switch oled", "oled"]),
    TechBenchmark("Nintendo Switch Lite", 10000, ["switch lite"]),
    TechBenchmark("Sony WH-1000XM4", 15000, ["wh-1000xm4", "wh1000xm4", "1000xm4"]),
    TechBenchmark("Sony WH-1000XM5", 22000, ["wh-1000xm5", "wh1000xm5", "1000xm5"]),
    TechBenchmark("Bose QuietComfort 45", 14000, ["qc45", "quietcomfort 45", "qc 45"]),
    TechBenchmark("Raspberry Pi 5", 8000, ["raspberry pi 5", "pi 5"]),
    TechBenchmark("Raspberry Pi 4", 5000, ["raspberry pi 4", "pi 4"]),
    TechBenchmark("Steam Deck", 30000, ["steam deck"]),
    # Vêtements forte liquidité
    TechBenchmark("Patagonia Synchilla", 9000, ["synchilla", "patagonia synchilla"]),
    TechBenchmark("Carhartt Detroit", 13000, ["detroit", "carhartt detroit"]),
]


class TechMarketService:
    """Service d'analyse d'arbitrage et de rentabilité pour produits Tech & Vêtements."""

    @staticmethod
    def find_benchmark(title: str, alert_name: str | None = None) -> TechBenchmark | None:
        """Trouve la cote de référence correspondante dans le catalogue."""
        search_text = f"{title} {alert_name or ''}".lower()
        search_norm = re.sub(r"[^\w\s]", " ", search_text)

        for bench in TECH_BENCHMARKS:
            for kw in bench.keywords:
                pattern = r"\b" + re.escape(kw) + r"\b"
                if re.search(pattern, search_norm):
                    return bench
        return None

    @classmethod
    def get_comparison_summary(
        cls,
        listing: Listing,
        alert: Alert | None = None,
    ) -> str | None:
        """Génère la ligne de comparatif de marché et de marge nette estimée.

        Zéro latence : calcul arithmétique pur en mémoire (< 0.1 ms).
        """
        if not listing.item_price_cents or listing.item_price_cents <= 0:
            return None

        alert_name = alert.name if alert else None
        bench = cls.find_benchmark(listing.title, alert_name=alert_name)

        if bench:
            resale_target_cents = bench.resale_target_cents
        elif alert and alert.max_item_price_cents:
            resale_target_cents = int(alert.max_item_price_cents * 1.35)
        else:
            return None

        vinted_price_cents = listing.item_price_cents

        # Ne pas afficher de marge négative ou nulle
        if vinted_price_cents >= resale_target_cents:
            return None

        # Frais de revente estimés (environ 5%)
        estimated_fees_cents = int(resale_target_cents * 0.05)
        net_profit_cents = resale_target_cents - vinted_price_cents - estimated_fees_cents

        if net_profit_cents <= 0:
            return None

        roi_percent = round((net_profit_cents / vinted_price_cents) * 100)
        resale_str = format_cents_to_euros(resale_target_cents)
        profit_str = format_cents_to_euros(net_profit_cents)

        return (
            f"📊 <b>Cote revente :</b> ~{resale_str}\n"
            f"💰 <b>Bénéfice estimé :</b> +{profit_str}"
        )
