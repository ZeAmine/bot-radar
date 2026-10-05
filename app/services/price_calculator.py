"""Service de calcul du montant total estimé d'une annonce (article, protection et port)."""

# Frais standards constatés sur Vinted France
DEFAULT_PROTECTION_FIXED_CENTS: int = 70  # 0,70 € fixe
DEFAULT_PROTECTION_RATE: float = 0.05  # 5% du prix de l'article
DEFAULT_SHIPPING_CENTS: int = 288  # 2,88 € en point relais standard (Mondial Relay / Shop2Shop)


def calculate_buyer_protection_cents(item_price_cents: int) -> int:
    """Calcule les frais de protection acheteur Vinted standards : 0,70 € + 5% du prix."""
    return DEFAULT_PROTECTION_FIXED_CENTS + round(item_price_cents * DEFAULT_PROTECTION_RATE)


def calculate_estimated_total(
    item_price_cents: int,
    estimated_protection_cents: int | None = None,
    estimated_shipping_cents: int | None = None,
    fallback_defaults: bool = False,
) -> int | None:
    """Calcule le total estimé en centimes (article + protection + livraison).

    Règles :
    - Si protection et port sont fournis : total calculé.
    - Si un frais est manquant et fallback_defaults=True : barème standard Vinted.
    - Si un frais est manquant et fallback_defaults=False : retourne None.
    """
    protection = estimated_protection_cents
    shipping = estimated_shipping_cents

    if fallback_defaults:
        if protection is None:
            protection = calculate_buyer_protection_cents(item_price_cents)
        if shipping is None:
            shipping = DEFAULT_SHIPPING_CENTS

    if protection is None or shipping is None:
        return None

    return item_price_cents + protection + shipping


def format_price_summary(
    item_price_cents: int,
    estimated_total_cents: int | None = None,
    fallback_to_default_fees: bool = True,
) -> str:
    """Formate la ligne d'affichage du prix pour Telegram.

    Le total inclut protection acheteur et port : ce n'est jamais un montant réel
    fourni par Vinted, il est donc toujours présenté comme une estimation.

    Exemples :
    - "Prix : 15,00 € — Total estimé : 19,33 € (protection et port inclus)"
    - "Prix : 15,00 €" (si le total est inconnu et le repli désactivé)
    """
    item_euros = f"{item_price_cents / 100:.2f} €".replace(".", ",")
    total_cents = estimated_total_cents

    if total_cents is None and fallback_to_default_fees:
        total_cents = calculate_estimated_total(
            item_price_cents=item_price_cents,
            fallback_defaults=True,
        )

    if total_cents is not None:
        total_euros = f"{total_cents / 100:.2f} €".replace(".", ",")
        return f"Prix : {item_euros} — Total estimé : {total_euros} (protection et port inclus)"

    return f"Prix : {item_euros}"
