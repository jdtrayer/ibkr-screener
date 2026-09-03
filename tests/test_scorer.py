"""compute_score is documented in config.py with worked numeric examples --
these pin those examples down as regression tests, so a weight change that
silently shifts the score curve gets caught rather than just re-observed
live next session."""
import pytest

from momentum_scanner.scorer import compute_score


def test_labt_like_mover_scores_around_5_5():
    # 1.5%/min move, $600K/min flow, 40% gap, 0.62% spread -- config.py: "+5.50"
    score = compute_score(1.5, 600_000, 40.0, 0.62)
    assert score == pytest.approx(5.50, abs=0.01)


def test_dead_name_scores_around_minus_2_83():
    # 0.05%/min move, $20K/min flow, 5% gap, 3% spread -- config.py: "-2.83"
    score = compute_score(0.05, 20_000, 5.0, 3.0)
    assert score == pytest.approx(-2.83, abs=0.01)


def test_unknown_gap_earns_no_bonus_but_does_not_block_scoring():
    with_gap = compute_score(1.0, 100_000, 10.0, 0.5)
    without_gap = compute_score(1.0, 100_000, None, 0.5)
    assert without_gap < with_gap


def test_unknown_spread_costs_nothing():
    with_spread = compute_score(1.0, 100_000, 10.0, 5.0)  # over the free allowance
    without_spread = compute_score(1.0, 100_000, 10.0, None)
    assert without_spread > with_spread
