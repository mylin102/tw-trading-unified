"""Contract & regression tests for single-basis executable-BBO spread_z + degenerate std fail-closed.

2026-11-25 Gemini CLI: Covers T1, T2, T3 (degenerate std + warmup merged), and T5
against the real FuturesMonitor._mts_tick -> TMFSpread.on_bar callsite.
"""
from datetime import datetime
from types import SimpleNamespace
import math
import time
import pytest

import strategies.futures.monitor as monitor_mod
from strategies.futures.monitor import FuturesMonitor
from strategies.plugins.futures.active import tmf_spread
from strategies.plugins.futures.active.tmf_spread import TMFSpread


class _StopAfterSpreadZ(Exception):
    pass


def _make_monitor(tmp_path, monkeypatch, *, csv_ma=-132.0, csv_std=11.720427511513673):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TRADING_RUNTIME_ID", raising=False)
    monkeypatch.delenv("MTS_STATE_PATH", raising=False)
    monkeypatch.delenv("MTS_BACKTEST", raising=False)
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open", lambda: True)
    monkeypatch.setattr(monitor_mod, "_mts_position_state_path", lambda: tmp_path / "no_state.json")
    monkeypatch.setattr(
        "core.channel_safety.get_safety_state",
        lambda: type("Safety", (), {"entry_allowed": lambda self, ticker: True,
                                    "entry_blocked_reason": ""})(),
    )
    monkeypatch.setattr(tmf_spread, "_maintenance_entry_lock_active", lambda: False)
    monkeypatch.setattr(tmf_spread, "_write_mts_state", lambda **kw: None)
    monkeypatch.setattr(tmf_spread, "_write_mts_telemetry", lambda **kw: None)
    monkeypatch.setattr(tmf_spread, "_append_event", lambda *a, **kw: None)

    mon = object.__new__(FuturesMonitor)
    mon.ticker = "TMF"
    mon.dry_run = True
    mon.live_trading = False
    mon.api = None
    mon.cfg = {"mts": {"enabled": True, "strategy": "tmf_spread", "params": {"entry_z": 3.0, "min_atr": 1.0}}}
    mon.trader = SimpleNamespace(position=0)
    mon._execution_context = SimpleNamespace(requested_mode="paper", effective_mode="paper_active")
    mon._oco_reconciled = True
    mon._last_far_snap_ts = time.time()
    mon._reconcile_pending_mts_combos = lambda: None
    mon._check_broker_snapshot_request = lambda: False
    mon._mts_has_open_position_from_fills = lambda: False
    mon._broker_truth_is_flat = lambda auth=None: True
    mon._spread_loaded = True
    mon._spread_loader = SimpleNamespace(
        enrich_bar=lambda b: b.update({"spread_ma": csv_ma, "spread_std": csv_std})
    )
    mon.market_data = {}

    events = []
    mon._append_mts_event = lambda event, **payload: events.append((event, payload))

    strat = TMFSpread()
    from core.strategy_context import MarketData, PositionView, StrategyContext
    strat.init(StrategyContext(
        position=PositionView(size=0),
        market=MarketData(ticker="TMF", last_bar={"near_close": 38715.0, "far_close": 38810.0, "atr": 20.0}),
        config={"ticker": "TMF", "params": {"entry_z": 3.0, "min_atr": 1.0}},
    ))
    strat._has_position = False
    strat._lifecycle = "FLAT"
    mon._registry = {"tmf_spread": strat}
    return mon, strat, events


def _run_mts_tick_and_capture(mon, bar_dict):
    """Run real FuturesMonitor._mts_tick up to _apply_spread_dynamics and return enriched bar."""
    captured = {}

    def _capture(b):
        captured.update(b)
        raise _StopAfterSpreadZ()

    mon._apply_spread_dynamics = _capture
    try:
        FuturesMonitor._mts_tick(mon, enriched_bar=dict(bar_dict))
    except _StopAfterSpreadZ:
        pass
    return captured


def _bbo_bar(*, near_close, near_bid, near_ask, far_close, far_bid, far_ask, atr=20.0, ts=None):
    return {
        "ts": ts or datetime(2026, 11, 25, 9, 20, 28),
        "timestamp": ts or datetime(2026, 11, 25, 9, 20, 28),
        "near_close_rt": near_close,
        "far_close_rt": far_close,
        "near_close": near_close,
        "far_close": far_close,
        "bbo_near_bid": near_bid,
        "bbo_near_ask": near_ask,
        "bbo_far_bid": far_bid,
        "bbo_far_ask": far_ask,
        "bbo_near_age_ms": 14,
        "bbo_far_age_ms": 13,
        "bbo_near_state": "OK",
        "bbo_far_state": "OK",
        "near_bid": near_bid,
        "near_ask": near_ask,
        "far_bid": far_bid,
        "far_ask": far_ask,
        "atr": atr,
    }


def _eval_strategy_entry(strat, enriched_bar):
    from core.strategy_context import MarketData, PositionView, StrategyContext
    ctx = StrategyContext(
        position=PositionView(size=0),
        market=MarketData(ticker="TMF", last_bar=enriched_bar),
        config={"ticker": "TMF", "params": {"entry_z": 3.0, "min_atr": 1.0}},
    )
    return strat.on_bar(ctx)


def _seed_history_around(mon, *, mid_mean=-132.0, half_cross=19.0, std_target=11.72, count=19):
    """Feed `count` executable-BBO ticks whose sell_exec / buy_exec have mean=(mid_mean-/+half_cross) and std≈std_target."""
    # Alternating offsets around 0 with sample std ≈ std_target
    offsets = [
        -1.0, 1.0, -1.5, 1.5, -0.5, 0.5, -1.2, 1.2, -0.8, 0.8,
        -1.1, 1.1, -0.9, 0.9, -1.3, 1.3, -0.7, 0.7, 0.0,
    ][:count]
    # Scale offsets so sample std matches std_target
    raw_var = sum(x * x for x in offsets) / max(len(offsets) - 1, 1)
    scale = std_target / math.sqrt(raw_var) if raw_var > 0 else 1.0
    for i, off in enumerate(offsets):
        mid_sp = mid_mean + off * scale
        nb, na = 38686.0, 38691.0  # width 5
        near_mid = (nb + na) / 2.0
        far_mid = near_mid - mid_sp
        fb, fa = far_mid - 16.5, far_mid + 16.5  # width 33 -> total half_cross = (5+33)/2 = 19.0
        _run_mts_tick_and_capture(
            mon,
            _bbo_bar(
                near_close=near_mid, near_bid=nb, near_ask=na,
                far_close=far_mid, far_bid=fb, far_ask=fa,
            ),
        )


def test_t1_order_000001_same_basis_produces_no_entry(tmp_path, monkeypatch):
    """T1: 2026-11-25 ORD-20261125-000001 actual quotes (near 38715/38686/38691, far 38810/38807/38840)
    must NOT trigger an entry under unified executable basis (z ≈ -0.26, not +3.16).
    """
    mon, strat, _ = _make_monitor(tmp_path, monkeypatch, csv_ma=-132.0, csv_std=11.720427511513673)
    _seed_history_around(mon, mid_mean=-132.0, half_cross=19.0, std_target=11.72, count=19)

    bar_000001 = _bbo_bar(
        near_close=38715.0, near_bid=38686.0, near_ask=38691.0,
        far_close=38810.0, far_bid=38807.0, far_ask=38840.0,
    )
    out = _run_mts_tick_and_capture(mon, bar_000001)

    assert out["spread_z"] is not None
    assert -0.45 < out["spread_z"] < -0.05, f"Expected z ≈ -0.26, got {out['spread_z']}"
    signal = _eval_strategy_entry(strat, out)
    assert signal is None


def test_t2_stats_and_observation_same_executable_basis_contract(tmp_path, monkeypatch):
    """T2: Rolling stats and current observation MUST come from the exact same executable-BBO
    sequence; diverging close prices must not affect spread/mean/std/z.
    """
    mon, _, _ = _make_monitor(tmp_path, monkeypatch, csv_ma=-132.0, csv_std=11.72)
    _seed_history_around(mon, mid_mean=-132.0, half_cross=19.0, std_target=10.0, count=19)

    # Deliberately inject a huge bogus close_spread (-50) while BBO sell_exec=-110 (genuine wide spread)
    nb, na, fb, fa = 38700.0, 38704.0, 38780.0, 38810.0  # sell_exec = -110, buy_exec = -76
    bar_a = _bbo_bar(near_close=38760.0, near_bid=nb, near_ask=na, far_close=38810.0, far_bid=fb, far_ask=fa)
    out = _run_mts_tick_and_capture(mon, bar_a)

    assert out["spread"] == nb - fa  # executable SELL_NEAR_BUY_FAR basis, NEVER near_close - far_close (-50)
    assert out["spread"] != (38760.0 - 38810.0)
    expected_z = (out["spread"] - out["spread_ma"]) / out["spread_std"]
    assert out["spread_z"] == pytest.approx(expected_z, rel=1e-12)


def test_t3_degenerate_std_and_insufficient_warmup_fail_closed_with_event(tmp_path, monkeypatch):
    """T3 (includes warmup T4): Both insufficient warmup samples (<20) and degenerate std
    (e.g. 3.22e-06) must refuse to emit spread_z (None, never clamped), block entry,
    and emit typed SPREAD_Z_UNAVAILABLE events.
    """
    # Part A: Warmup insufficient (<20 samples)
    mon_w, strat_w, events_w = _make_monitor(tmp_path, monkeypatch, csv_ma=-132.0, csv_std=11.72)
    out_w = _run_mts_tick_and_capture(
        mon_w,
        _bbo_bar(
            near_close=38715.0, near_bid=38686.0, near_ask=38691.0,
            far_close=38810.0, far_bid=38807.0, far_ask=38840.0,
        ),
    )
    assert out_w["spread_z"] is None
    assert _eval_strategy_entry(strat_w, out_w) is None
    assert any(
        ev == "SPREAD_Z_UNAVAILABLE" and p.get("reason") == "WARMUP_INSUFFICIENT"
        for ev, p in events_w
    )

    # Part B: Degenerate std ≈ 3.22e-06 (20 nearly identical executable BBO observations)
    mon_d, strat_d, events_d = _make_monitor(tmp_path, monkeypatch, csv_ma=-132.0, csv_std=3.22e-06)
    for i in range(19):
        eps = 1e-5 if (i % 2 == 0) else 0.0
        _run_mts_tick_and_capture(
            mon_d,
            _bbo_bar(
                near_close=38700.0, near_bid=38690.0 + eps, near_ask=38695.0 + eps,
                far_close=38850.0, far_bid=38830.0, far_ask=38840.0,
            ),
        )
    # 20th tick with tiny perturbation -> sample std ~ 5e-6 << 1.0
    out_d = _run_mts_tick_and_capture(
        mon_d,
        _bbo_bar(
            near_close=38700.0, near_bid=38690.0 + 1e-5, near_ask=38695.0 + 1e-5,
            far_close=38850.0, far_bid=38830.0, far_ask=38840.0,
        ),
    )
    assert out_d["spread_z"] is None
    assert _eval_strategy_entry(strat_d, out_d) is None
    assert any(
        ev == "SPREAD_Z_UNAVAILABLE" and p.get("reason") == "STD_DEGENERATE"
        for ev, p in events_d
    )


def test_t5_normal_same_basis_extreme_z_still_triggers_entry(tmp_path, monkeypatch):
    """T5: Normal same-basis window with healthy std and genuine executable-BBO extreme
    triggers SELL_NEAR_BUY_FAR (z > 3.0) and BUY_NEAR_SELL_FAR (z < -3.0) normally.
    """
    # Positive extreme -> SELL_NEAR_BUY_FAR
    mon_s, strat_s, _ = _make_monitor(tmp_path, monkeypatch, csv_ma=-132.0, csv_std=5.0)
    _seed_history_around(mon_s, mid_mean=-132.0, half_cross=19.0, std_target=5.0, count=19)
    # sell_exec mean ≈ -151, std ≈ 5 -> sell_exec = -125 gives z ≈ +4.0
    bar_sell = _bbo_bar(
        near_close=38700.0, near_bid=38700.0, near_ask=38705.0,
        far_close=38810.0, far_bid=38795.0, far_ask=38825.0,  # sell_exec = -125
    )
    out_s = _run_mts_tick_and_capture(mon_s, bar_sell)
    assert out_s["spread_z"] is not None and out_s["spread_z"] >= 3.0
    sig_s = _eval_strategy_entry(strat_s, out_s)
    assert sig_s is not None and sig_s.action == "SELL_NEAR_BUY_FAR"

    # Negative extreme -> BUY_NEAR_SELL_FAR
    mon_b, strat_b, _ = _make_monitor(tmp_path, monkeypatch, csv_ma=-132.0, csv_std=5.0)
    _seed_history_around(mon_b, mid_mean=-132.0, half_cross=19.0, std_target=5.0, count=19)
    # buy_exec mean ≈ -113, std ≈ 5 -> buy_exec = -140 gives z ≈ -4.0
    bar_buy = _bbo_bar(
        near_close=38680.0, near_bid=38675.0, near_ask=38680.0,
        far_close=38835.0, far_bid=38820.0, far_ask=38850.0,  # buy_exec = -140
    )
    out_b = _run_mts_tick_and_capture(mon_b, bar_buy)
    assert out_b["spread_z"] is not None and out_b["spread_z"] <= -3.0
    sig_b = _eval_strategy_entry(strat_b, out_b)
    assert sig_b is not None and sig_b.action == "BUY_NEAR_SELL_FAR"
