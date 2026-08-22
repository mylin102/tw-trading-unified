import csv
import json
from datetime import datetime, timedelta

import pytest

from scripts.research.mts_trend_replay_v1 import (
    ARMS, MIN_ELIGIBLE_FOR_APPROVAL, agg_5m, build_manifest, cost,
    entry_trend_mapping, leg_pnl, run_replay,
)
from strategies.plugins.futures.active.mts_trend_signal_adapter import TrendDirection


def _bars(start="2026-01-05 09:00:00", n=66):
    out = {}
    t = datetime.fromisoformat(start)
    for i in range(n):
        ts = t + timedelta(minutes=i)
        close = 46000.0 + i * 3
        out[ts.strftime("%Y-%m-%d %H:%M:%S")] = {
            "ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": close,
            "high": close + 2, "low": close - 2, "close": close,
            "volume": 100.0, "leg": "NEAR",
        }
    return out


def _write_synthetic(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    fills = [
        {"trade_id": "t1", "fill_type": "ENTRY", "leg": "NEAR", "side": "SHORT", "timestamp": "2026-01-05 09:00:00", "price": 46000.0},
        {"trade_id": "t1", "fill_type": "ENTRY", "leg": "FAR", "side": "LONG", "timestamp": "2026-01-05 09:00:00", "price": 46100.0},
        {"trade_id": "t1", "fill_type": "EXIT", "leg": "NEAR", "side": "BUY", "timestamp": "2026-01-05 10:00:00", "price": 45950.0},
        {"trade_id": "t2", "fill_type": "ENTRY", "leg": "NEAR", "side": "LONG", "timestamp": "2026-01-05 09:10:00", "price": 46100.0},
        {"trade_id": "t2", "fill_type": "ENTRY", "leg": "FAR", "side": "SHORT", "timestamp": "2026-01-05 09:10:00", "price": 46200.0},
        {"trade_id": "t2", "fill_type": "EXIT", "leg": "FAR", "side": "BUY", "timestamp": "2026-01-05 10:00:00", "price": 46050.0},
    ]
    fills_path = tmp_path / "fills.jsonl"; fills_path.write_text("\n".join(json.dumps(x) for x in fills))
    for leg in ("near", "far"):
        with open(data / f"tmf_{leg}_20260105.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["ts", "Open", "High", "Low", "Close", "Volume", "Amount"])
            for b in _bars().values(): w.writerow([b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"], 0])
    return fills_path


def test_5m_aggregation_warmup_and_partial_exclusion():
    bars = _bars(n=66); keys = sorted(bars)
    result = agg_5m(keys, bars, "NEAR", keys[0], keys[-1])
    assert len(result) == 13
    assert len(result[:12]) == 12
    assert result[0]["ts"] == "2026-01-05 09:04:00"
    assert result[-1]["ts"] == "2026-01-05 10:04:00"
    partial = agg_5m(keys, bars, "NEAR", keys[0], "2026-01-05 10:03:00")
    assert all(b["ts"] != "2026-01-05 10:04:00" for b in partial)


def test_entry_side_mapping_is_fail_closed():
    assert entry_trend_mapping({"entry_near": {"side": "LONG"}, "entry_far": {"side": "SHORT"}}) == (TrendDirection.BULLISH, "FAR")
    assert entry_trend_mapping({"entry_near": {"side": "SHORT"}, "entry_far": {"side": "LONG"}}) == (TrendDirection.BEARISH, "NEAR")
    assert entry_trend_mapping({"entry_near": {"side": "LONG"}, "entry_far": {"side": "LONG"}}) == (None, None)


def test_replay_has_no_lookahead_and_hold_without_confirmation(tmp_path):
    fills = _write_synthetic(tmp_path)
    res = run_replay(fills_path=fills, bars_root=str(tmp_path))
    assert res["eligible"] == 2
    assert res["verdict"] == "HOLD"
    assert res["status"] == "TREND_UNTESTED_NO_CONFIRM"
    assert res["arms"]["TREND_CONFIRMED_RELEASE"]["release"] == 0
    assert res["block_reason_distribution"]
    assert res["episode_first_confirm_or_block"]
    for ep in res["episode_first_confirm_or_block"].values():
        assert "decision_ts" in ep
        assert ep.get("execution_ts") is None or ep["execution_ts"] > ep["decision_ts"]


def test_manifest_telemetry_and_shared_cost(tmp_path):
    res = run_replay(fills_path=_write_synthetic(tmp_path), bars_root=str(tmp_path))
    manifest = build_manifest(res["arms"], res["eligible"], res["coverage"],
                              block_reason_distribution=res["block_reason_distribution"],
                              episode_first_confirm_or_block=res["episode_first_confirm_or_block"],
                              status=res["status"], verdict=res["verdict"], gates=res["gates"])
    assert manifest["content_sha256"] and len(manifest["content_sha256"]) == 64
    assert manifest["block_reason_distribution"]
    assert "episode_first_confirm_or_block" in manifest
    assert cost(100.0, 100.0) > 0
    assert leg_pnl(100.0, 110.0, "LONG") > 0
    assert leg_pnl(110.0, 100.0, "SHORT") > 0


def test_no_fills_is_fail_closed(tmp_path):
    res = run_replay(fills_path="", bars_root=str(tmp_path))
    assert res["verdict"] == "HOLD"
    assert res["error"] == "no fills log found"
    assert MIN_ELIGIBLE_FOR_APPROVAL == 30
    assert set(ARMS) == {"BASELINE_SINGLE_LEG_RELEASE", "TREND_CONFIRMED_RELEASE", "NO_REVT"}


def test_manifest_round_trip_json(tmp_path):
    res = run_replay(fills_path=_write_synthetic(tmp_path), bars_root=str(tmp_path))
    m = build_manifest(res["arms"], res["eligible"], res["coverage"])
    json.dumps(m)
    assert m["verdict"] == "HOLD"


def test_expected_side_does_not_depend_on_future_prices():
    # Mapping is entry-only; no price series is accepted by the API.
    expected, released = entry_trend_mapping({"entry_near": {"side": "LONG"}, "entry_far": {"side": "SHORT"}})
    assert expected == TrendDirection.BULLISH and released == "FAR"
