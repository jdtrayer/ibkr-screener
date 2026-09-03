"""Admission/eviction decision logic in filters.py -- deliberately pure
(no IB calls), so these run without a live connection. is_dead_on_bump in
particular is the fix for the TYA false-positive-admission bug
(backlog_2026_09_02 #4): a regression here would silently let a genuinely
dead symbol burn a live slot and an RVOL rebuild every re-entry cycle."""
from datetime import datetime, timedelta

from momentum_scanner import config
from momentum_scanner.filters import (
    PersistenceTracker,
    ScorerAdmission,
    bump_candidate,
    is_dead_on_bump,
)
from momentum_scanner.models import SymbolState
from momentum_scanner.scanner import ScanHit
from momentum_scanner.scorer import ScoreRow
from momentum_scanner.session import Session
from momentum_scanner.tunables import Tunables

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=config.TZ)


def make_state(symbol="TEST", dollar_volume=None, spread_pct=None, subscribed_seconds_ago=999, spiked_seconds_ago=None):
    state = SymbolState(symbol=symbol)
    if dollar_volume is not None:
        state.tick.last = 1.0
        state.volume_offset = 0.0
        state.tick.volume = dollar_volume  # price=1.0 => dollar_volume == volume
    if spread_pct is not None:
        # spread_pct = 200*(ask-bid)/(ask+bid) -- solve for ask given bid=100
        k = (1 + spread_pct / 200) / (1 - spread_pct / 200)
        state.tick.bid = 100.0
        state.tick.ask = 100.0 * k
    state.subscribed_at = NOW - timedelta(seconds=subscribed_seconds_ago)
    if spiked_seconds_ago is not None:
        state.spike.last_spike_at = NOW - timedelta(seconds=spiked_seconds_ago)
    return state


# -- is_dead_on_bump (backlog_2026_09_02 #4) --------------------------------

def test_dead_on_bump_true_at_near_zero_dollar_volume():
    # TYA's actual observed pattern: $0-$1,383 against a $5M regular-session floor.
    state = make_state(dollar_volume=1383)
    assert is_dead_on_bump(state, Session.REGULAR, config.DEAD_DV_FRACTION) is True


def test_dead_on_bump_false_for_a_real_dip_just_under_floor():
    # 80% of the floor -- weak enough to get bumped, but not "genuinely dead".
    floor = config.MIN_DOLLAR_VOLUME
    state = make_state(dollar_volume=floor * 0.8)
    assert is_dead_on_bump(state, Session.REGULAR, config.DEAD_DV_FRACTION) is False


def test_dead_on_bump_false_when_bumped_for_spread_not_volume():
    # Healthy dollar volume, but a wide spread -- bumped for spread, not deadness.
    state = make_state(dollar_volume=config.MIN_DOLLAR_VOLUME * 2, spread_pct=5.0)
    assert is_dead_on_bump(state, Session.REGULAR, config.DEAD_DV_FRACTION) is False


def test_dead_on_bump_true_when_dollar_volume_unknown():
    state = make_state(dollar_volume=None)
    assert is_dead_on_bump(state, Session.REGULAR, config.DEAD_DV_FRACTION) is True


# -- bump_candidate ----------------------------------------------------------

def test_bump_candidate_picks_the_weakest_offender():
    states = {
        "HEALTHY": make_state("HEALTHY", dollar_volume=config.MIN_DOLLAR_VOLUME * 10),
        "WEAK": make_state("WEAK", dollar_volume=config.MIN_DOLLAR_VOLUME * 0.5),
        "DEAD": make_state("DEAD", dollar_volume=100),
    }
    picked = bump_candidate(states, Session.REGULAR, NOW)
    assert picked is not None and picked.symbol == "DEAD"


def test_bump_candidate_none_when_everyone_clears_the_floor():
    states = {
        "A": make_state("A", dollar_volume=config.MIN_DOLLAR_VOLUME * 5),
        "B": make_state("B", dollar_volume=config.MIN_DOLLAR_VOLUME * 3),
    }
    assert bump_candidate(states, Session.REGULAR, NOW) is None


def test_bump_candidate_excludes_symbols_still_in_warmup():
    states = {
        "TOO_NEW": make_state("TOO_NEW", dollar_volume=1, subscribed_seconds_ago=1),
    }
    assert bump_candidate(states, Session.REGULAR, NOW) is None


def test_bump_candidate_excludes_recently_spiked_symbols():
    states = {
        "SPIKED": make_state("SPIKED", dollar_volume=1, spiked_seconds_ago=5),
    }
    assert bump_candidate(states, Session.REGULAR, NOW) is None


# -- PersistenceTracker -------------------------------------------------------

def test_persistence_tracker_qualifies_after_required_consecutive_hits():
    tunables = Tunables()
    tunables.persistence_required = 3
    tunables.persistence_top_n = 15
    tracker = PersistenceTracker(tunables)

    hit = ScanHit(symbol="AAA", con_id=1, rank=1, source="TEST")
    assert tracker.update({"AAA": hit}) == set()
    assert tracker.update({"AAA": hit}) == set()
    assert tracker.update({"AAA": hit}) == {"AAA"}


def test_persistence_tracker_does_not_qualify_below_top_n():
    tunables = Tunables()
    tunables.persistence_top_n = 15
    tracker = PersistenceTracker(tunables)
    hit = ScanHit(symbol="AAA", con_id=1, rank=50, source="TEST")  # below top-15
    assert tracker.update({"AAA": hit}) == set()


# -- ScorerAdmission -----------------------------------------------------------

def make_score_row(symbol, score=2.0, fast_lane=False):
    return ScoreRow(
        symbol=symbol, score=score, move_pct_per_min=1.0, dollar_per_min=100_000,
        gap_pct=5.0, spread_pct=0.5, fast_lane=fast_lane,
    )


def test_scorer_admission_requires_consecutive_sweeps_before_admitting():
    tunables = Tunables()
    tunables.scorer_reserved_slots = 3
    tunables.scorer_admit_min_score = 1.0
    admission = ScorerAdmission(tunables)

    row = make_score_row("BTQ")
    assert admission.update([row], already_live=set()) == []  # sweep 1: not yet
    assert admission.update([row], already_live=set()) == [row]  # sweep 2: admitted


def test_scorer_admission_fast_lane_admits_immediately():
    tunables = Tunables()
    tunables.scorer_reserved_slots = 3
    tunables.scorer_admit_min_score = 1.0
    admission = ScorerAdmission(tunables)

    row = make_score_row("BTQ", fast_lane=True)
    assert admission.update([row], already_live=set()) == [row]


def test_scorer_admission_excludes_below_min_score():
    tunables = Tunables()
    tunables.scorer_admit_min_score = 1.0
    admission = ScorerAdmission(tunables)

    row = make_score_row("WOOF", score=-0.03)  # observed live 2026-09-02
    admission.update([row], already_live=set())
    assert admission.update([row], already_live=set()) == []
