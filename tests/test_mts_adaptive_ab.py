"""2026-10-16 Hermes Agent: paired observations, real net-outcome metrics and CLI."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import math

import pytest

from strategies.plugins.futures.active.mts_adaptive_ab import (
    compare_outcomes, evaluate_hvwap_ab, log_ab_pair, main, paired_record,
)
from strategies.plugins.futures.active.mts_adaptive_vwap import AdaptiveVWAPConfig
from strategies.plugins.futures.active.mts_oi_estimator import OISnapshot


TS = datetime(2026, 10, 15, 10, tzinfo=timezone(timedelta(hours=8))).timestamp()


def inputs():
    return dict(decision_ts=TS,
        near_bars=[{"ts": TS - 300, "close": 100, "volume": 1000}, {"ts": TS, "close": 110, "volume": 1}],
        far_bars=[{"ts": TS - 300, "close": 90, "volume": 1}, {"ts": TS, "close": 91, "volume": 1}],
        near_price=110, far_price=91, near_side="LONG", far_side="SHORT", atr_15m=10,
        oi_snapshot=OISnapshot(TS, 1, 2, .9, 10, 0, 9, 99),
        event_sequence=[{"side": "buy", "order_id": "A"}, {"side": "sell", "order_id": "B"}])


def outcomes():
    monday = datetime(2026, 6, 8, 10, tzinfo=timezone(timedelta(hours=8))).timestamp()
    return [{"pair_id": str(i), "ts": monday + i * 86400,
             "baseline": {"net_pnl": a}, "adaptive": {"net_pnl": b}}
            for i, (a, b) in enumerate(zip([-100, 200, -50, 100, 50], [-40, 210, 20, 110, 60]))]


def test_paired_arm_price_and_baseline_arbitration_parity():
    arms = evaluate_hvwap_ab(**inputs())
    a, b = arms["baseline"], arms["adaptive"]
    assert a.adaptive_price is None
    assert b.adaptive_price is not None
    assert a.near.vwap <= b.adaptive_price <= b.adaptive_twap
    assert replace(b, adaptive_price=None, chips_strength=None, alpha=None, adaptive_twap=None) == a
    reused = evaluate_hvwap_ab(baseline=a, **inputs())
    assert reused == arms
    from strategies.plugins.futures.active.mts_hvwap_candidate import evaluate_hvwap_candidate
    assert b == evaluate_hvwap_candidate(**inputs(), use_adaptive=True)


def test_record_and_locked_daily_collector(tmp_path):
    arms = evaluate_hvwap_ab(**inputs())
    record = paired_record("pair-A", arms)
    assert record["adaptive_data_valid"] is True
    assert record["adaptive"]["exit_executed"] is False
    assert record["baseline"]["exit_price"] is None
    assert record["release_action_emitted"] is False
    path = log_ab_pair(record, tmp_path / "paper" / "ab_test")
    log_ab_pair(dict(record, pair_id="pair-B"), tmp_path / "paper" / "ab_test")
    from pathlib import Path
    rows = [json.loads(line) for line in Path(path).read_text().splitlines()]
    assert [row["pair_id"] for row in rows] == ["pair-A", "pair-B"]
    assert path.endswith("paired_20261015.jsonl")
    with pytest.raises(ValueError):
        log_ab_pair(dict(record, decision_ts="bad"), tmp_path)


def test_hypothetical_exit_marks_are_not_actual_fills():
    from strategies.plugins.futures.active.mts_hvwap_candidate import HvwapStatus
    arms = evaluate_hvwap_ab(**inputs())
    near = replace(arms["baseline"], status=HvwapStatus.ALIGNED_PASS, hypothetical_release_leg="NEAR")
    far = replace(near, hypothetical_release_leg="FAR")
    record = paired_record("marks", {"baseline": near, "adaptive": far})
    assert record["baseline"]["candidate_release_eligible"] is True
    assert record["baseline"]["hypothetical_exit_price"] == near.near.price
    assert record["adaptive"]["hypothetical_exit_price"] == far.far.price
    assert record["adaptive"]["exit_price"] is None


def test_net_metrics_initial_peak_and_five_day_gate():
    report = compare_outcomes(list(reversed(outcomes())), initial_equity=1000)
    assert report["baseline"]["win_rate_pct"] == 60
    assert report["adaptive"]["win_rate_pct"] == 80
    assert report["baseline"]["max_drawdown_pct"] == -10
    assert report["adaptive"]["max_drawdown_pct"] == -4
    assert report["mdd_reduction_pct"] == 60
    assert report["win_rate_gain_pp"] == 20
    assert report["baseline"]["final_equity"] == 1200
    assert math.isfinite(report["baseline"]["sharpe"])
    assert report["sharpe_gain"] is not None
    assert report["promotion"] == "GO_CANDIDATE"
    assert report["status"] == "EVALUATED"


def test_insufficient_or_failed_promotion_is_no_go():
    assert compare_outcomes([], initial_equity=1000)["status"] == "INSUFFICIENT_DATA"
    assert compare_outcomes(outcomes()[:4], initial_equity=1000)["promotion"] == "NO_GO"
    same_day = [dict(row, ts=TS) for row in outcomes()]
    assert compare_outcomes(same_day, initial_equity=1000)["promotion"] == "NO_GO"
    weekend_span = [dict(row, ts=TS + i * 86400) for i, row in enumerate(outcomes())]
    assert compare_outcomes(weekend_span, initial_equity=1000)["status"] == "INSUFFICIENT_DATA"
    cfg = AdaptiveVWAPConfig(minimum_win_rate_gain_pp=50)
    assert compare_outcomes(outcomes(), initial_equity=1000, config=cfg)["promotion"] == "NO_GO"


def test_zero_drawdown_zero_variance_and_insolvency_are_explicit():
    rows = [{"pair_id": str(i), "ts": TS + i * 86400,
             "baseline": {"net_pnl": 0}, "adaptive": {"net_pnl": 0}} for i in range(5)]
    report = compare_outcomes(rows, initial_equity=1000)
    assert report["mdd_reduction_pct"] is None
    assert report["baseline"]["sharpe"] is None
    assert report["sharpe_gain"] is None
    assert report["promotion"] == "NO_GO"
    rows[0]["baseline"]["net_pnl"] = -2000
    assert compare_outcomes(rows, initial_equity=1000)["baseline"]["sharpe"] is None


def test_simultaneous_closes_do_not_manufacture_drawdown():
    rows = [{"pair_id": "A", "ts": TS, "baseline": {"net_pnl": 100}, "adaptive": {"net_pnl": 100}},
            {"pair_id": "B", "ts": TS, "baseline": {"net_pnl": -100}, "adaptive": {"net_pnl": -100}}]
    assert compare_outcomes(rows, initial_equity=1000)["baseline"]["max_drawdown_pct"] == 0


def test_overflowing_derived_ratios_fail_loudly():
    rows = outcomes()[:2]
    rows[0]["baseline"]["net_pnl"] = -1e308
    with pytest.raises(ValueError, match="drawdown"):
        compare_outcomes(rows, initial_equity=1e-308)
    rows[0]["baseline"]["net_pnl"] = 1e308
    with pytest.raises(ValueError, match="daily return"):
        compare_outcomes(rows, initial_equity=1e-308)


@pytest.mark.parametrize("pair", [{}, {"pair_id": "A", "ts": TS, "baseline": {}},
    {"pair_id": "", "ts": TS, "baseline": {"net_pnl": 1}, "adaptive": {"net_pnl": 1}},
    {"pair_id": "A", "ts": 1e308, "baseline": {"net_pnl": 1}, "adaptive": {"net_pnl": 1}}])
def test_invalid_unpaired_outcomes_are_rejected(pair):
    with pytest.raises(ValueError):
        compare_outcomes([pair], initial_equity=1000)


def test_duplicates_invalid_capital_and_overflow_are_rejected():
    with pytest.raises(ValueError):
        compare_outcomes([outcomes()[0], outcomes()[0]], initial_equity=1000)
    for capital in (0, None, float("inf")):
        with pytest.raises(ValueError):
            compare_outcomes([], initial_equity=capital)
    rows = outcomes()[:2]
    for row in rows:
        row["baseline"]["net_pnl"] = 1e308
    with pytest.raises(ValueError):
        compare_outcomes(rows, initial_equity=1e308)


def test_comparison_cli_writes_json(tmp_path):
    path, output = tmp_path / "pairs.jsonl", tmp_path / "reports" / "comparison.json"
    path.write_text("\n".join(json.dumps(row) for row in outcomes()) + "\n\n")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("minimum_trading_days: 5\n")
    assert main(["--input", str(path), "--output", str(output), "--initial-equity", "1000",
                 "--config", str(cfg)]) == 0
    assert json.loads(output.read_text())["promotion"] == "GO_CANDIDATE"
    assert main(["--input", str(path), "--output", str(output), "--initial-equity", "1000"]) == 0
