"""Utilitaires purs de formatage monétaire.

Formater un prix est une règle d'affichage, pas une logique métier : une seule
implémentation, consommée par les services comme par les claviers du bot, si
bien qu'un prix ne peut pas s'écrire « 15,00 € » d'un côté et autre chose de
l'autre.
"""


def format_cents(cents: int) -> str:
    """Formate des centimes en euros : 1500 → « 15,00 € »."""
    return f"{cents / 100:.2f} €".replace(".", ",")
