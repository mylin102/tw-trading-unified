"""Phase 2 + Phase 3 variant tests (2026-08-22).

Phase 2 — frozen replay variants (shared cost model, pre-registered params):
  * STRICT            : CHOP regime/exit VETOES (spec CHOP->BLOCK matrix)
  * TOLERANT          : P1 behavior — CHOP does not veto
  * TOLERANT_VELOCITY : TOLERANT + 15m spread-velocity confirmation
  All variants share the SAME cost model (BROKER_FEE/TAX_RATE/POINT_VALUE)
  and the SAME fill/execution semantics (next completed bar close).

Phase 3 — multi-window walk-forward OOS:
  * deterministic date split, LAST window = OOS, no eval-window tuning
  * verdict HOLD when OOS loses to baseline; ROBUST when it beats it
"""
import csv
import json
from datetime import datetime, timedelta

import pytest

import scripts.research.mts_trend_replay_v1 as h
from strategies.plugins.futures.active.mts_trend_signal_adapter import TrendDirection


def _bar(ts, close, open_=None):
    o = close if open_ is None else open_
    return {"ts": ts, "open": o, "high": close + 2.0, "low": close - 2.0,
            "close": close, "volume": 100.0, "leg": "NEAR"}


def _gen_bars(start, n, close_fn):
    out = {}
    t = datetime.fromisoformat(start)
    for i in range(n):
        ts = t + timedelta(minutes=i)
        c = close_fn(i)
        s = ts.strftime("%Y-%m-%d %H:%M:%S")
        out[s] = _bar(s, c)
    return out


def _uptrend_bars(start="2026-01-05 08:00:00", n=240, slope=1.5, base=46000.0):
    return _gen_bars(start, n, lambda i: base + i * slope)


def _flat_bars(start="2026-01-05 08:00:00", n=240):
    return _gen_bars(start, n, lambda i: 46000.0)


def _rising_then_flat_15m():
    """Rising 60m block whose LAST completed 15m block is flat (CHOP)."""
    out = {}
    t = datetime.fromisoformat("2026-01-05 08:00:00")
    for i in range(240):
        ts = t + timedelta(minutes=i)
        close = 46000.0 + i * 10.0 if i < 10 else 46100.0
        open_ = (46000.0 + i * 10.0 - 1.0) if i < 10 else 46100.0
        s = ts.strftime("%Y-%m-%d %H:%M:%S")
        out[s] = _bar(s, close, open_=open_)
    return out


def _bull_ep():
    return {"entry_near": {"side": "LONG"}, "entry_far": {"side": "SHORT"}}


def _fake_pass(decision_ts, near_series, expected):
    return {"decision_ts": decision_ts, "direction": expected.value, "confidence": 1.0,
            "pass_release": True, "block_reason": None,
            "signal_timestamps": {"adl": decision_ts, "renko": decision_ts, "vwap": decision_ts}}


def _write_csv(data_dir, leg, bars):
    with open(data_dir / f"tmf_{leg}_20260105.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["ts", "Open", "High", "Low", "Close", "Volume", "Amount"])
        for b in bars.values():
            w.writerow([b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"], 0])


def _write_synthetic(tmp_path, n_episodes=2):
    """Single-date fills + bars (BEARISH entries: near SHORT / far LONG)."""
    data = tmp_path / "data"; data.mkdir()
    bars = _uptrend_bars("2026-01-05 08:00:00", 240)
    fills = []
    for i in range(n_episodes):
        base = 46000.0 + i * 100.0
        fills += [
            {"trade_id": f"t{i}", "fill_type": "ENTRY", "leg": "NEAR", "side": "SHORT", "timestamp": "2026-01-05 09:00:00", "price": base},
            {"trade_id": f"t{i}", "fill_type": "ENTRY", "leg": "FAR", "side": "LONG", "timestamp": "2026-01-05 09:00:00", "price": base + 100.0},
            {"trade_id": f"t{i}", "fill_type": "EXIT", "leg": "NEAR", "side": "BUY", "timestamp": "2026-01-05 10:00:00", "price": base - 50.0},
        ]
    fills_path = tmp_path / "fills.jsonl"
    fills_path.write_text("\n".join(json.dumps(x) for x in fills))
    for leg in ("near", "far"):
        _write_csv(data, leg, bars)
    return fills_path


def _write_two_day_synthetic(tmp_path, eps_per_date=5):
    """Two-date fills + bars (BULLISH entries) for walk-forward windows."""
    data = tmp_path / "data"; data.mkdir()
    day1 = _uptrend_bars("2026-01-05 08:00:00", 240)
    day2 = _uptrend_bars("2026-01-06 08:00:00", 240)
    fills = []
    for d, bars in (("2026-01-05", day1), ("2026-01-06", day2)):
        for i in range(eps_per_date):
            base = 46000.0 + i * 100.0
            fills += [
                {"trade_id": f"t_{d}_{i}", "fill_type": "ENTRY", "leg": "NEAR", "side": "LONG", "timestamp": f"{d} 09:00:00", "price": base},
                {"trade_id": f"t_{d}_{i}", "fill_type": "ENTRY", "leg": "FAR", "side": "SHORT", "timestamp": f"{d} 09:00:00", "price": base + 100.0},
                {"trade_id": f"t_{d}_{i}", "fill_type": "EXIT", "leg": "NEAR", "side": "SELL", "timestamp": f"{d} 10:00:00", "price": base},
            ]
    fills_path = tmp_path / "fills.jsonl"
    fills_path.write_text("\n".join(json.dumps(x) for x in fills))
    for leg in ("near", "far"):
        with open(data / f"tmf_{leg}_20260105.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["ts", "Open", "High", "Low", "Close", "Volume", "Amount"])
            for b in day1.values(): w.writerow([b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"], 0])
        with open(data / f"tmf_{leg}_20260106.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["ts", "Open", "High", "Low", "Close", "Volume", "Amount"])
            for b in day2.values(): w.writerow([b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"], 0])
    return fills_path


# ---- Phase 2: variant gate semantics --------------------------------------

def test_variant_params_pre_registered_and_immutable():
    assert set(h.VARIANT_PARAMS) == {"STRICT", "TOLERANT", "TOLERANT_VELOCITY"}
    assert h.VARIANT_PARAMS["STRICT"]["chop_vetoes"] is True
    assert h.VARIANT_PARAMS["TOLERANT"]["chop_vetoes"] is False
    assert h.VARIANT_PARAMS["TOLERANT_VELOCITY"]["chop_vetoes"] is False
    assert h.VARIANT_PARAMS["TOLERANT_VELOCITY"]["velocity_check"] is True
    assert h.VARIANT_PARAMS["TOLERANT_VELOCITY"]["velocity_min_abs_slope_pts"] > 0
    with pytest.raises(TypeError):
        h.VARIANT_PARAMS["STRICT"]["chop_vetoes"] = False
    with pytest.raises(TypeError):
        h.VARIANT_PARAMS["STRICT"]["NEW_PARAM"] = 1
    with pytest.raises(TypeError):
        h.VARIANT_PARAMS["BOGUS"] = {}


def test_strict_vetoes_chop_regime_tolerant_does_not(monkeypatch):
    """STRICT blocks a CHOP 60m regime; TOLERANT lets the pipeline decide."""
    monkeypatch.setattr(h, "_trend_decision", _fake_pass)
    bars = _flat_bars(); keys = sorted(bars)
    ep = _bull_ep()
    strict = h.walk_trend_confirmation(keys, bars, "2026-01-05 09:00:00", "2026-01-05 10:00:00",
                                       ep=ep, variant="STRICT")
    assert isinstance(strict, dict)
    assert strict.get("block_reason") == "INSUFFICIENT_SAME_DIRECTION"
    assert not strict.get("execution_bar")
    tol = h.walk_trend_confirmation(keys, bars, "2026-01-05 09:00:00", "2026-01-05 10:00:00",
                                    ep=ep, variant="TOLERANT")
    assert isinstance(tol, dict)
    assert tol.get("pass_release") is True
    assert tol.get("execution_bar") is not None
    assert tol["execution_ts"] > tol["decision_ts"]


def test_strict_vetoes_chop_exit_tolerant_does_not(monkeypatch):
    """STRICT blocks a CHOP 15m exit block; TOLERANT does not veto CHOP exits."""
    monkeypatch.setattr(h, "_trend_decision", _fake_pass)
    bars = _rising_then_flat_15m(); keys = sorted(bars)
    ep = _bull_ep()
    strict = h.walk_trend_confirmation(keys, bars, "2026-01-05 09:00:00", "2026-01-05 10:00:00",
                                       ep=ep, variant="STRICT")
    assert strict.get("block_reason") == "INSUFFICIENT_SAME_DIRECTION"
    assert not strict.get("execution_bar")
    tol = h.walk_trend_confirmation(keys, bars, "2026-01-05 09:00:00", "2026-01-05 10:00:00",
                                    ep=ep, variant="TOLERANT")
    assert tol.get("pass_release") is True
    assert tol.get("execution_bar") is not None


def test_tolerant_velocity_flat_velocity_blocks():
    """Identical near/far series -> zero spread slope -> VELOCITY_FLAT block."""
    near = _uptrend_bars(n=240)
    far = _uptrend_bars(n=240, slope=1.5)  # identical -> spread constant
    keys = sorted(near)
    res = h.walk_trend_confirmation(keys, near, "2026-01-05 09:00:00", "2026-01-05 10:00:00",
                                    ep=_bull_ep(), variant="TOLERANT_VELOCITY",
                                    far_bars=far, far_keys=sorted(far))
    assert isinstance(res, dict)
    assert res.get("block_reason") == "VELOCITY_FLAT"
    assert not res.get("execution_bar")


def test_tolerant_velocity_aligned_velocity_confirms():
    """Near rises faster than far -> spread widens -> BULLISH velocity confirms."""
    near = _uptrend_bars(n=240, slope=1.5)
    far = _uptrend_bars(n=240, slope=1.0)
    keys = sorted(near)
    res = h.walk_trend_confirmation(keys, near, "2026-01-05 09:00:00", "2026-01-05 10:00:00",
                                    ep=_bull_ep(), variant="TOLERANT_VELOCITY",
                                    far_bars=far, far_keys=sorted(far))
    assert isinstance(res, dict)
    assert res.get("pass_release") is True
    assert res.get("execution_bar") is not None
    assert res.get("velocity_15m", {}).get("aligned") is True


def test_tolerant_velocity_opposite_velocity_blocks():
    """Far rises faster than near -> spread shrinks -> VELOCITY_OPPOSITE block."""
    near = _uptrend_bars(n=240, slope=1.5)
    far = _uptrend_bars(n=240, slope=2.0)
    keys = sorted(near)
    res = h.walk_trend_confirmation(keys, near, "2026-01-05 09:00:00", "2026-01-05 10:00:00",
                                    ep=_bull_ep(), variant="TOLERANT_VELOCITY",
                                    far_bars=far, far_keys=sorted(far))
    assert isinstance(res, dict)
    assert res.get("block_reason") == "VELOCITY_OPPOSITE"
    assert not res.get("execution_bar")


def test_all_variants_share_cost_model(tmp_path):
    """Every variant uses the SAME pessimistic cost model and execution path."""
    fills = _write_synthetic(tmp_path, n_episodes=6)
    s1 = h.run_replay(fills_path=fills, bars_root=str(tmp_path), variant="STRICT")
    s2 = h.run_replay(fills_path=fills, bars_root=str(tmp_path), variant="TOLERANT")
    s3 = h.run_replay(fills_path=fills, bars_root=str(tmp_path), variant="TOLERANT_VELOCITY")
    assert h.SHARED_COST == {"broker_fee": 20.0, "tax_rate": 2e-5, "point_value": 10.0}
    for r in (s1, s2, s3):
        assert r["shared_cost"] == h.SHARED_COST
        assert r["variant_params"] == dict(h.VARIANT_PARAMS[r["variant"]])
        # Variant-independent arms are bit-identical across variants.
        assert r["arms"]["BASELINE_SINGLE_LEG_RELEASE"]["pnl"] == s1["arms"]["BASELINE_SINGLE_LEG_RELEASE"]["pnl"]
        assert r["arms"]["NO_REVT"]["pnl"] == s1["arms"]["NO_REVT"]["pnl"]
        assert r["eligible"] == s1["eligible"]
        assert r["coverage"] == s1["coverage"]


def test_run_all_variants_keys_and_json_safe(tmp_path):
    fills = _write_synthetic(tmp_path, n_episodes=6)
    res = h.run_all_variants(fills_path=fills, bars_root=str(tmp_path))
    assert set(res["variants"]) == {"STRICT", "TOLERANT", "TOLERANT_VELOCITY"}
    assert res["shared_cost"] == h.SHARED_COST
    assert len(res["content_sha256"]) == 64
    json.dumps(res)  # JSON-safe: no MappingProxyType leaks into manifests
    for v, d in res["variants"].items():
        assert d["params"] == dict(h.VARIANT_PARAMS[v])
        assert d["eligible"] == res["eligible"]
        assert "trend_release" in d and "trend_pnl" in d


def test_unknown_variant_fail_closed():
    bars = _flat_bars(); keys = sorted(bars)
    with pytest.raises(ValueError):
        h.walk_trend_confirmation(keys, bars, "2026-01-05 09:00:00", "2026-01-05 10:00:00",
                                  ep=_bull_ep(), variant="BOGUS")
    with pytest.raises(ValueError):
        h.run_replay(variant="BOGUS")
    with pytest.raises(ValueError):
        h.run_walk_forward(variant="BOGUS")


# ---- Phase 3: multi-window walk-forward OOS --------------------------------

def test_walk_forward_oos_split_no_eval_window_tuning(tmp_path):
    fills_path = _write_two_day_synthetic(tmp_path)
    fills = h.load_fills(str(fills_path))
    windows = h.split_windows(fills, 2)
    assert [w["name"] for w in windows] == ["W1", "W2"]
    all_dates = [d for w in windows for d in w["dates"]]
    assert len(all_dates) == len(set(all_dates))          # disjoint windows
    assert set(all_dates) == {"2026-01-05", "2026-01-06"}  # full coverage
    assert windows[-1]["oos"] is True                      # LAST = OOS
    assert windows[0]["oos"] is False
    res = h.run_walk_forward(fills_path=fills_path, bars_root=str(tmp_path), variant="TOLERANT")
    assert res["no_eval_window_tuning"] is True            # params never re-fit
    assert res["variant_params"] == dict(h.VARIANT_PARAMS["TOLERANT"])
    assert len(res["windows"]) == 2
    assert res["windows"][-1]["oos"] is True
    assert res["oos"]["name"] == "W2"
    assert res["oos"]["coverage"] >= 0.9
    assert res["oos"]["release"] > 0


def test_walk_forward_hold_when_oos_loses_to_baseline(tmp_path, monkeypatch):
    fills_path = _write_two_day_synthetic(tmp_path)

    def _bad_walk(keys, bars, ts_from, ts_to, expected=None, ep=None, **kwargs):
        exp = expected or TrendDirection.BULLISH
        return {"execution_bar": {"close": 99999.0}, "pass_release": True,
                "direction": exp.value, "decision_ts": ts_from, "block_reason": None}

    monkeypatch.setattr(h, "walk_trend_confirmation", _bad_walk)
    res = h.run_walk_forward(fills_path=fills_path, bars_root=str(tmp_path), variant="TOLERANT")
    assert res["verdict"] == "HOLD"
    assert "oos_pnl_not_above_baseline" in res["verdict_reason"]
    assert res["oos"]["release"] == 5
    assert res["oos"]["pnl"] < res["oos"]["baseline_pnl"]


def test_walk_forward_robust_when_oos_beats_baseline(tmp_path, monkeypatch):
    fills_path = _write_two_day_synthetic(tmp_path)

    def _good_walk(keys, bars, ts_from, ts_to, expected=None, ep=None, **kwargs):
        exp = expected or TrendDirection.BULLISH
        return {"execution_bar": {"close": 45000.0}, "pass_release": True,
                "direction": exp.value, "decision_ts": ts_from, "block_reason": None}

    monkeypatch.setattr(h, "walk_trend_confirmation", _good_walk)
    res = h.run_walk_forward(fills_path=fills_path, bars_root=str(tmp_path), variant="TOLERANT")
    assert res["verdict"] == "ROBUST"
    assert res["verdict_reason"] == "all_oos_conditions_met"
    assert res["oos"]["pnl"] > res["oos"]["baseline_pnl"]
    assert res["oos"]["release"] > 0
    assert res["oos"]["coverage"] >= 0.9
