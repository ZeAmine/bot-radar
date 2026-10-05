"""Tests du percentile interpolé partagé."""

import pytest

from app.utils.percentile import interpolated_percentile


def test_interpolated_percentile_on_empty_sample() -> None:
    """Sans échantillon, il n'y a pas de percentile : None, pas de 0."""
    assert interpolated_percentile([], 50) is None


def test_interpolated_percentile_single_sample() -> None:
    """Un seul échantillon est son propre percentile."""
    assert interpolated_percentile([17.4], 50) == pytest.approx(17.4)
    assert interpolated_percentile([17.4], 95) == pytest.approx(17.4)


def test_interpolated_percentile_reaches_the_extremes() -> None:
    """p0 et p100 sont les extrêmes, comme `percentile_cont`."""
    samples = [1.0, 2.0, 3.0, 4.0]
    assert interpolated_percentile(samples, 0) == pytest.approx(1.0)
    assert interpolated_percentile(samples, 100) == pytest.approx(4.0)


def test_interpolated_percentile_interpolates_linearly() -> None:
    """Entre deux valeurs encadrantes, proportionnellement au rang."""
    assert interpolated_percentile([0.98, 4.09], 50) == pytest.approx(2.535)
    assert interpolated_percentile([0.98, 4.09], 95) == pytest.approx(3.9345)
    assert interpolated_percentile([10.0, 20.0, 30.0], 50) == pytest.approx(20.0)
    assert interpolated_percentile([10.0, 20.0, 30.0], 90) == pytest.approx(28.0)


def test_interpolated_percentile_does_not_require_sorted_input() -> None:
    """L'ordre d'entrée est indifférent : l'échantillon est trié sur place."""
    assert interpolated_percentile([30.0, 10.0, 20.0], 50) == pytest.approx(20.0)


def test_interpolated_percentile_rejects_out_of_range_percentage() -> None:
    """Un pourcentage hors bornes est une faute de programme, pas une mesure muette."""
    with pytest.raises(ValueError, match="hors bornes"):
        interpolated_percentile([1.0], 101)
    with pytest.raises(ValueError, match="hors bornes"):
        interpolated_percentile([1.0], -1)