# 2026-08-22 TSB 2.0: small test exercising the counterfactual replay harness
# scripts/research/mts_trend_replay_v1.py against a tiny synthetic dataset.
import json

import pytest

from scripts.research.mts_trend_replay_v1 import (
    run_replay,
    build_manifest,
    cost,
    leg_pnl,
    ARMS,
    MIN_ELIGIBLE_FOR_APPROVAL,
)


def _write_synthetic(tmp_path):
    """Write a tiny fills log + near/far bar CSVs under tmp_path and return
    the fills path. Enough for 2 eligible episodes (< 30 -> HOLD verdict)."""
    data = tmp_path / "data"
    data.mkdir()
    fills = [
        # episode 1
        {"trade_id": "t1", "fill_type": "ENTRY", "leg": "NEAR", "side": "SHORT",
         "timestamp": "2026-01-05 09:00:00", "price": 46000.0},
        {"trade_id": "t1", "fill_type": "ENTRY", "leg": "FAR", "side": "LONG",
         "timestamp": "2026-01-05 09:00:00", "price": 46100.0},
        {"trade_id": "t1", "fill_type": "EXIT", "leg": "NEAR", "side": "BUY",
         "timestamp": "2026-01-05 10:00:00", "price": 45950.0},
        # episode 2
        {"trade_id": "t2", "fill_type": "ENTRY", "leg": "NEAR", "side": "SHORT",
         "timestamp": "2026-01-05 09:10:00", "price": 46100.0},
        {"trade_id": "t2", "fill_type": "ENTRY", "leg": "FAR", "side": "LONG",
         "timestamp": "2026-01-05 09:10:00", "price": 46200.0},
        {"trade_id": "t2", "fill_type": "RELEASE", "leg": "FAR", "side": "SELL",
         "timestamp": "2026-01-05 09:40:00", "price": 46150.0},
        {"trade_id": "t2", "fill_type": "EXIT", "leg": "NEAR", "side": "BUY",
         "timestamp": "2026-01-05 10:00:00", "price": 46050.0},
    ]
    fills_path = tmp_path / "fills.jsonl"
    fills_path.write_text("\n".join(json.dumps(f) for f in fills))

    # continuous 1-min bars 09:00..10:00 for near & far
    for leg in ("near", "far"):
        lines = ["ts,Open,High,Low,Close,Volume,Amount"]
        t = 9 * 60
        while t <= 10 * 60:
            hh, mm = divmod(t, 60)
            ts = f"2026-01-05 {hh:02d}:{mm:02d}:00"
            base = 46100.0 if leg == "far" else 46000.0
            close = base + (t - 9 * 60) * 3.0
            lines.append(f"{ts},{close:.1f},{close + 2:.1f},{close - 2:.1f},{close:.1f},100,0")
            t += 1
        (data / f"tmf_{leg}_20260105.csv").write_text("\n".join(lines))
    return fills_path


def test_run_replay_structure_and_verdict(tmp_path):
    fills_path = _write_synthetic(tmp_path)
    res = run_replay(fills_path=fills_path, bars_root=str(tmp_path))
    assert "arms" in res
    assert set(res["arms"].keys()) == set(ARMS)
    for arm, s in res["arms"].items():
        assert s["eligible"] >= 0
        assert "pnl" in s and "avg_pnl" in s and "max_drawdown" in s
        assert "release" in s and "combined" in s and "exit_count" in s
        assert "coverage" in s
    # tiny sample -> HOLD / RESEARCH_INSUFFICIENT_SAMPLE
    assert res["eligible"] < MIN_ELIGIBLE_FOR_APPROVAL
    assert res["verdict"] == "HOLD"
    assert res["status"] == "RESEARCH_INSUFFICIENT_SAMPLE"


def test_run_replay_ready_when_enough_eligible(tmp_path, monkeypatch):
    fills_path = _write_synthetic(tmp_path)
    monkeypatch.setattr("scripts.research.mts_trend_replay_v1.MIN_ELIGIBLE_FOR_APPROVAL", 1)
    res = run_replay(fills_path=(fills_path), bars_root=str(tmp_path))
    assert res["eligible"] >= 1
    assert res["verdict"] == "READY_FOR_APPROVAL"
    assert res["status"] == "OBSERVED"


def test_manifest_and_shared_cost(tmp_path):
    fills_path = _write_synthetic(tmp_path)
    res = run_replay(fills_path=str(fills_path), bars_root=str(tmp_path))
    manifest = build_manifest(res["arms"], res["eligible"], res["coverage"])
    assert manifest["harness"] == "mts_trend_replay_v1"
    assert manifest["content_sha256"] and len(manifest["content_sha256"]) == 64
    assert "coverage_per_arm" in manifest
    assert set(manifest["coverage_per_arm"].keys()) == set(ARMS)
    # shared cost model: a 10-pt winning long leg must be +; short oppposite -cost on flat
    assert cost(100.0, 100.0) > 0                      # pure cost, no move
    assert leg_pnl(100.0, 110.0, "LONG") > 0           # +10 pts long profitable
    assert leg_pnl(110.0, 100.0, "SHORT") > 0          # -10 pts short profitable