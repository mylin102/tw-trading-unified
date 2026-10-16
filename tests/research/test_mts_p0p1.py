"""P0/P1 regression tests (2026-08-22):
P0 — pre-entry warmup: agg_5m from (entry-60min) yields 12 completed 5m bars
     BEFORE the entry bar, so ADL has warmup at the first post-entry decision.
P1 — loose 2-of-3: a CHOP regime/exit does NOT veto when the pipeline's
     arbitration would pass; only a true opposite blocks.
"""
from datetime import datetime, timedelta

from scripts.research.mts_trend_replay_v1 import (
    agg_5m, entry_trend_mapping, walk_trend_confirmation,
)
from strategies.plugins.futures.active.mts_trend_signal_adapter import TrendDirection


def _uptrend_bars(start="2026-01-05 08:00:00", n=180):
    """1-min bars with a steady uptrend (for a bullish expected direction)."""
    out = {}
    t = datetime.fromisoformat(start)
    for i in range(n):
        ts = t + timedelta(minutes=i)
        close = 46000.0 + i * 1.5
        out[ts.strftime("%Y-%m-%d %H:%M:%S")] = {
            "ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": close - 1.0,
            "high": close + 2.0, "low": close - 2.0, "close": close,
            "volume": 100.0 + i, "leg": "NEAR",
        }
    return out


def test_p0_preentry_warmup_yields_12_bars_before_entry():
    """P0: aggregating from entry-60min must give >=12 completed 5m bars before
    the first post-entry decision bar (ADL warmup available at entry)."""
    bars = _uptrend_bars()
    keys = sorted(bars)
    entry_ts = "2026-01-05 09:00:00"
    warmup_from = "2026-01-05 08:00:00"       # entry - 60 min
    horizon = "2026-01-05 09:30:00"
    five = agg_5m(keys, bars, "NEAR", warmup_from, horizon)
    pre_entry = [b for b in five if datetime.fromisoformat(b["asof_ts"]) < datetime.fromisoformat(entry_ts)]
    assert len(pre_entry) >= 12, f"expected >=12 pre-entry completed bars, got {len(pre_entry)}"


def test_p0_no_decision_before_entry():
    """P0: the walk must never fire a decision on a pre-entry bar."""
    bars = _uptrend_bars()
    keys = sorted(bars)
    entry_ts = "2026-01-05 09:00:00"
    horizon = "2026-01-05 09:40:00"
    ep = {"entry_near": {"side": "SHORT"}, "entry_far": {"side": "LONG"}}  # BEARISH expected
    res = walk_trend_confirmation(keys, bars, entry_ts, horizon, ep=ep)
    telemetry = res.get("telemetry") if isinstance(res, dict) else []
    for t in telemetry:
        if isinstance(t, dict) and t.get("decision_ts"):
            assert datetime.fromisoformat(t["decision_ts"]) >= datetime.fromisoformat(entry_ts)


def test_p1_loose_2of3_confirms_on_aligned_uptrend():
    """P1: a monotonic uptrend aligned with BULLISH expected must confirm under
    the loose gate (pre-warmup + CHOP-no-veto)."""
    bars = _uptrend_bars(n=240)
    keys = sorted(bars)
    entry_ts = "2026-01-05 09:00:00"
    horizon = "2026-01-05 10:00:00"
    ep = {"entry_near": {"side": "LONG"}, "entry_far": {"side": "SHORT"}}  # BULLISH expected
    res = walk_trend_confirmation(keys, bars, entry_ts, horizon, ep=ep)
    assert isinstance(res, dict)
    assert res.get("pass_release") is True, (
        f"expected trend confirmation on aligned uptrend; got {res.get('block_reason')}"
    )


def test_p1_entry_side_mapping_still_fail_closed():
    """P1 keeps D4: same-side or missing entries fail closed."""
    assert entry_trend_mapping({"entry_near": {"side": "LONG"}, "entry_far": {"side": "LONG"}}) == (None, None)
    assert entry_trend_mapping({"entry_near": {"side": "SHORT"}, "entry_far": {"side": "LONG"}}) == (TrendDirection.BEARISH, "NEAR")