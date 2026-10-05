"""Percentile interpolé, règles pures. Aucun I/O.

La même lecture — « que vaut le p50, le p90, le p95 de ces mesures ? » — revient à
deux endroits du projet : la latence de notification et la cadence de scrutation. Deux
implémentations de la même interpolation finiraient par diverger, alors que ces deux
nombres s'affichent dans le même message `/status` : ils doivent se lire pareil.

L'interpolation est celle de `percentile_cont` (PostgreSQL) : entre les deux valeurs
encadrantes, proportionnellement au rang. Le premier et le dernier échantillon sont
donc atteints exactement, et un échantillon unique renvoie sa valeur.
"""

from collections.abc import Sequence


def interpolated_percentile(values: Sequence[float], pct: float) -> float | None:
    """Percentile interpolé de `values`, ou None si l'échantillon est vide.

    `values` n'a pas besoin d'être trié. `pct` est exprimé en pourcentage (0 à 100).
    """
    if not values:
        return None
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"percentile hors bornes : {pct}")

    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)