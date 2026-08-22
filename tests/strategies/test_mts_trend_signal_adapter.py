# 2026-08-22 TSB 2.0: focused unit tests for the MTS 2.0 trend-confirmation
# signal adapter (ADL-SNR + Renko + Micro-VWAP arbitration) and the lifecycle
# TREND_RELEASE integration. Uses only the real module APIs from
# strategies.plugins.futures.active.* — no broker, no filesystem, no Shioaji.
import pytest

from strategies.plugins.futures.active.mts_trend_signal_adapter import (
    TrendDirection,
    SubSignalState,
    compute_adl_snr,
    adl_signal_state,
    arbitrate_trend,
    ADL_SNR_THRESHOLD,
    S_RENKO_SAME,
    S_ADL_SAME,
    S_VWAP_NEUTRAL,
    S_VWAP_OPPOSITE,
    S_RENKO_NONE,
    S_ADL_CHOP,
)
from strategies.plugins.futures.active.mts_renko_signal import (
    compute_renko,
    RenkoState,
)
from strategies.plugins.futures.active.mts_micro_vwap import (
    compute_micro_vwap,
    MicroVWAPResult,
)

# --- ADL-SNR -------------------------------------------------------------

WINDOW_N = 12


def _bullish_bars(n: int = WINDOW_N) -> list[dict]:
    """n strictly-trending bullish OHLCV bars (close near high, volume>0).

    Volume increases each bar so the ADL line is slightly convex — this
    guarantees a NONZERO linear-regression residual. A perfectly straight
    ADL would force residual_std == 0, which compute_adl_snr maps to SNR 0
    (CHOP) by its fail-closed guard.
    """
    bars = []
    for i in range(n):
        base = 100.0 + i * 0.5
        bars.append({
            "high": base + 0.6,
            "low": base - 0.2,
            "close": base + 0.4,   # MFM>0 -> ADL increasing -> bullish
            "volume": 100.0 + i * 1.0,
        })
    return bars


def test_adl_warmup_blocks():
    """Fewer than window_n completed bars -> fail-closed UNKNOWN (spec 5.3)."""
    res = compute_adl_snr("2026-01-09 10:00:00", _bullish_bars(5), window_n=WINDOW_N)
    assert res.direction == TrendDirection.UNKNOWN
    assert res.n_bars == 5
    assert res.snr == 0.0
    ss = adl_signal_state(res, TrendDirection.BULLISH)
    assert ss.score == -1.0       # UNKNOWN -> fail-closed
    assert ss.direction == TrendDirection.UNKNOWN


def test_adl_bullish():
    """12 bullish trend bars -> BULLISH with |snr| above the ADL_SNR_THRESHOLD."""
    res = compute_adl_snr("2026-01-09 10:00:00", _bullish_bars(WINDOW_N), window_n=WINDOW_N)
    assert res.direction == TrendDirection.BULLISH
    assert res.snr > ADL_SNR_THRESHOLD
    assert res.n_bars == WINDOW_N
    ss = adl_signal_state(res, TrendDirection.BULLISH)
    assert ss.direction == TrendDirection.BULLISH
    assert ss.score == S_ADL_SAME


def test_adl_bearish_expected_must_match():
    """A bullish ADL must NOT score as same for an expected BEARISH leg."""
    res = compute_adl_snr("2026-01-09 10:00:00", _bullish_bars(WINDOW_N), window_n=WINDOW_N)
    assert res.direction == TrendDirection.BULLISH
    ss = adl_signal_state(res, TrendDirection.BEARISH)
    assert ss.score == -1.0       # opposite
    assert ss.direction == TrendDirection.BULLISH


# --- Arbitration ---------------------------------------------------------

def test_arbitrate_boost():
    """Renko SAME (1.0) + ADL SAME (1.0) + VWAP NEUTRAL (0.5) ->
    Confidence 0.45+0.35+0.10 = 0.90 >= 0.70 -> pass_release True, BULLISH."""
    renko = SubSignalState(source="renko", direction=TrendDirection.BULLISH, score=S_RENKO_SAME)
    adl = SubSignalState(source="adl", direction=TrendDirection.BULLISH, score=S_ADL_SAME)
    vwap = SubSignalState(source="vwap", direction=TrendDirection.CHOP, score=S_VWAP_NEUTRAL)
    d = arbitrate_trend("2026-01-09T10:00:00", renko, adl, vwap,
                        decision_max_quote_age_ms=0.0, window_max_quote_age_ms=0.0)
    assert d.pass_release is True
    assert d.direction == TrendDirection.BULLISH
    assert d.confidence == pytest.approx(0.90)
    assert d.block_reason is None


def test_arbitrate_divergence_blocks():
    """A reverse VWAP sub-signal (score -1.0) fails closed -> BLOCK even when
    Renko + ADL agree on the direction."""
    renko = SubSignalState(source="renko", direction=TrendDirection.BULLISH, score=S_RENKO_SAME)
    adl = SubSignalState(source="adl", direction=TrendDirection.BULLISH, score=S_ADL_SAME)
    vwap = SubSignalState(source="vwap", direction=TrendDirection.BEARISH, score=S_VWAP_OPPOSITE)
    d = arbitrate_trend("2026-01-09T10:00:00", renko, adl, vwap)
    assert d.pass_release is False
    assert d.block_reason == "DIVERGENCE_OR_INSUFFICIENT"
    assert d.confidence == pytest.approx(0.45 + 0.35 - 0.20)  # 0.60


def test_arbitrate_insufficient_blocks():
    """Fewer than 2 same-direction sub-signals -> INSUFFICIENT_SAME_DIRECTION.
    Renko FLAT (0.0) + ADL CHOP (0.5) + VWAP NEUTRAL (0.5): no directional
    consensus -> block even though no single sub-signal is negative."""
    renko = SubSignalState(source="renko", direction=TrendDirection.CHOP, score=S_RENKO_NONE)
    adl = SubSignalState(source="adl", direction=TrendDirection.CHOP, score=S_ADL_CHOP)
    vwap = SubSignalState(source="vwap", direction=TrendDirection.CHOP, score=S_VWAP_NEUTRAL)
    d = arbitrate_trend("2026-01-09T10:00:00", renko, adl, vwap)
    assert d.pass_release is False
    assert d.block_reason == "INSUFFICIENT_SAME_DIRECTION"


def test_arbitrate_reverse_vwap_with_unknown_fails_closed():
    """Any UNKNOWN or negative sub-signal fails closed regardless of the
    positives — the reverse/unknown VWAP arm always vetoes."""
    for vwap in (
        SubSignalState(source="vwap", direction=TrendDirection.BEARISH, score=S_VWAP_OPPOSITE),
        SubSignalState(source="vwap", direction=TrendDirection.UNKNOWN, score=S_VWAP_OPPOSITE),
    ):
        renko = SubSignalState(source="renko", direction=TrendDirection.BULLISH, score=S_RENKO_SAME)
        adl = SubSignalState(source="adl", direction=TrendDirection.BULLISH, score=S_ADL_SAME)
        d = arbitrate_trend("2026-01-09T10:00:00", renko, adl, vwap)
        assert d.pass_release is False
        assert d.block_reason == "DIVERGENCE_OR_INSUFFICIENT"


# --- Renko -----------------------------------------------------------------

def test_renko_2_ups():
    """compute_renko prices=[100,115,125], brick=10 -> UP with 2 consecutive
    same-direction bricks."""
    r = compute_renko("2026-01-09T10:00:00", [100.0, 115.0, 125.0], 10.0)
    assert r.direction == RenkoState.UP
    assert r.consecutive_same_direction == 2
    assert r.n_bricks == 2
    assert r.brick_reverse is False


def test_renko_signal_state_fail_closed_insufficient():
    """< 2 bricks -> FLAT -> S_RENKO_NONE, never a pass."""
    r = compute_renko("2026-01-09T10:00:00", [100.0, 105.0, 109.0], 10.0)
    assert r.direction == RenkoState.FLAT
    assert r.consecutive_same_direction == 0


# --- Lifecycle TREND_RELEASE ------------------------------------------------
def test_trend_release_lifecycle_positive():
    """trend_release_enabled=True + trend_confirmed pass_release=True +
    release_leg FAR on an ARMED/INACTIVE SPREAD position -> TREND_RELEASE decision
    releasing the FAR counter-trend leg."""
    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        PositionLifecycle,
        PositionPhase,
        LifecycleContext,
        LifecycleAction,
        evaluate_lifecycle_actions,
    )
    ctx = LifecycleContext(
        near_pnl_pts=10.0,
        far_pnl_pts=-5.0,
        floating_pnl_pts=5.0,
        entry_age_secs=100.0,
        release_stop_threshold=20.0,
        trail_dist=10.0,
        trend_release_enabled=True,
        trend_confirmed={
            "decision_ts": "2026-01-09T10:00:00",
            "pass_release": True,
            "release_leg": "FAR",
        },
    )
    lc = PositionLifecycle(phase=PositionPhase.SPREAD)   # release_group INACTIVE
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is not None
    assert decision.action == LifecycleAction.TREND_RELEASE
    assert decision.release_leg is not None and decision.release_leg.value == "FAR"
    assert decision.winner == "TREND_CONFIRMED_RELEASE"


def test_trend_release_lifecycle_blocks():
    """trend_release_enabled=False OR trend_confirmed=None -> BLOCK (no action)."""
    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        PositionLifecycle,
        PositionPhase,
        LifecycleContext,
        evaluate_lifecycle_actions,
    )
    lc = PositionLifecycle(phase=PositionPhase.SPREAD)
    # disabled
    ctx_disabled = LifecycleContext(
        0.0, 0.0, 0.0, 0.0, 20.0, 10.0,
        trend_release_enabled=False,
        trend_confirmed={"pass_release": True, "release_leg": "FAR"},
    )
    assert evaluate_lifecycle_actions(ctx_disabled, lc) is None
    # trend_confirmed None
    ctx_none = LifecycleContext(
        0.0, 0.0, 0.0, 0.0, 20.0, 10.0,
        trend_release_enabled=True,
        trend_confirmed=None,
    )
    assert evaluate_lifecycle_actions(ctx_none, lc) is None