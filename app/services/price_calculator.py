"""Service de calcul du montant total estimé d'une annonce."""


def calculate_estimated_total(
    item_price_cents: int,
    estimated_protection_cents: int | None,
    estimated_shipping_cents: int | None,
) -> int | None:
    """Calcule le total estimé en centimes (article + protection + livraison).

    Règles strictes :
    - Si protection et port sont connus : total calculé.
    - Si une des deux valeurs est inconnue (None) : total null (None).
    """
    if estimated_protection_cents is None or estimated_shipping_cents is None:
        return None

    return item_price_cents + estimated_protection_cents + estimated_shipping_cents


def format_price_summary(
    item_price_cents: int,
    estimated_total_cents: int | None,
) -> str:
    """Formate la ligne d'affichage du prix pour Telegram.

    Exemples :
    - "💵 9,00 € (Total : 13,69 €)"
    - "💵 9,00 € (Total : à confirmer)"
    """
    item_euros = f"{item_price_cents / 100:.2f} €".replace(".", ",")
    if estimated_total_cents is not None:
        total_euros = f"{estimated_total_cents / 100:.2f} €".replace(".", ",")
        return f"💵 {item_euros} (Total : {total_euros})"
    return f"💵 {item_euros} (Total : à confirmer)"
