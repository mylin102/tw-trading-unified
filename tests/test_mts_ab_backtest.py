"""2026-10-16 Hermes Agent: proxy execution timing, missing outcomes and exact joins."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from scripts.research import compare_ab_results as report
from scripts.research import run_adaptive_ab_backtest as replay


def snapshot(path, *, future_close=130, gap=False):
    lines = ["ts,Open,High,Low,Close,Volume,Amount"]
    start = datetime(2026, 7, 6, 9, 10)
    for bucket, (opening, closing) in enumerate(((100, 100), (105, 110), (120, future_close))):
        if gap and bucket == 2:
            continue
        for minute in range(5):
            ts = start + timedelta(minutes=5 * bucket + minute)
            lines.append(f"{ts},{opening},{max(opening, closing)},{min(opening, closing)},{closing},93,0")
    path.write_text("\n".join(lines) + "\n")
    return path


def study(tmp_path, *, future_close=130, gap=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    near = snapshot(tmp_path / "near.csv", future_close=future_close, gap=gap)
    far = snapshot(tmp_path / "far.csv", future_close=future_close, gap=gap)
    output = tmp_path / "output"
    replay.run_study(near, far, start_date="2026-07-06", end_date="2026-07-06", output_dir=output)
    a = report.load_arm(sorted(output.glob("baseline_*.jsonl")), "baseline")
    b = report.load_arm(sorted(output.glob("adaptive_*.jsonl")), "adaptive")
    return output, a, b


def test_next_bar_open_fill_and_known_pnl(tmp_path):
    output, a, b = study(tmp_path)
    middle = sorted(a.values(), key=lambda r: r["ts"])[1]
    assert middle["entry_price"] == 120
    assert middle["entry_price"] != middle["near_close"]
    assert middle["entry_ts"] == middle["ts"]
    assert middle["exit_ts"] == middle["ts"] + 300
    assert middle["gross_pnl_points"] == 10
    result = report.compare(a, b, initial_equity=40000)
    assert result["decision_pairs"] == 3
    assert result["paired_count"] == 2
    assert result["ties"] == 2
    assert result["baseline"]["simulated_trades"] == 1
    assert result["baseline"]["trade_win_rate_pct"] == 100
    assert result["baseline"]["win_rate_pct"] == 50
    assert result["baseline"]["gross_pnl_points"] == 10
    assert result["baseline"]["sharpe"] is None
    assert result["promotion"] == "NO_GO"
    assert all(r["signals"]["oi_source"] == "ASSUMED_CONSTANT_NOT_OBSERVED" for r in b.values())
    assert all(not r["real_order_emitted"] for r in b.values())
    with pytest.raises(ValueError, match="already exists"):
        replay.run_study(output.parent / "near.csv", output.parent / "far.csv",
            start_date="2026-07-06", end_date="2026-07-06", output_dir=output)


def test_future_mutation_cannot_change_current_signal(tmp_path):
    _, a, b = study(tmp_path / "first")
    _, other_a, other_b = study(tmp_path / "changed", future_close=80)
    for arm, other in ((a, other_a), (b, other_b)):
        current = sorted(arm.values(), key=lambda r: r["ts"])[1]
        changed = other[current["pair_id"]]
        for key in ("vwap", "twap", "reference_price", "alpha", "signals", "signal_direction"):
            assert current[key] == changed[key]
        assert current["gross_pnl_points"] == 10
        assert changed["gross_pnl_points"] == -40


def test_gaps_keep_null_not_zero(tmp_path):
    _, a, b = study(tmp_path, gap=True)
    last = sorted(b.values(), key=lambda r: r["ts"])[-1]
    assert last["gross_pnl_points"] is None
    assert not last["outcome_evaluable"]
    assert last["unavailable_reason"] == "NEXT_PAIRED_BAR_MISSING"
    assert report.compare(a, b, initial_equity=40000)["paired_count"] == 1


@pytest.mark.parametrize("field", ["ts", "near_close", "far_close", "signals", "outcome_evaluable", "exit_ts"])
def test_input_mismatches_rejected(tmp_path, field):
    _, a, b = study(tmp_path)
    bad = deepcopy(b)
    bad[next(iter(bad))][field] = "mismatch"
    with pytest.raises(ValueError, match="mismatch"):
        report.compare(a, bad, initial_equity=40000)


def test_missing_and_duplicate_pairs_rejected(tmp_path):
    output, a, b = study(tmp_path)
    bad = deepcopy(b)
    bad.pop(next(iter(bad)))
    with pytest.raises(ValueError, match="unpaired"):
        report.compare(a, bad, initial_equity=40000)
    path = next(output.glob("baseline_*.jsonl"))
    path.write_text(path.read_text() * 2)
    with pytest.raises(ValueError, match="duplicate"):
        report.load_arm([path], "baseline")


def test_comparator_cli_and_checksum_rejection(tmp_path):
    output, _, _ = study(tmp_path)
    args = ["--baseline", str(output / "baseline_*.jsonl"), "--adaptive", str(output / "adaptive_*.jsonl"),
            "--output", str(tmp_path / "comparison.md")]
    report.main(args)
    assert "NO_GO" in (tmp_path / "comparison.md").read_text()
    path = next(output.glob("baseline_*.jsonl"))
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="checksum"):
        report.main(args)


def test_source_mutation_rejected(tmp_path):
    output, _, _ = study(tmp_path)
    near = tmp_path / "near.csv"
    near.write_text(near.read_text() + "\n")
    with pytest.raises(ValueError, match="source checksum"):
        report.main(["--baseline", str(output / "baseline_*.jsonl"), "--adaptive",
                     str(output / "adaptive_*.jsonl"), "--output", str(tmp_path / "out.md")])


def test_uncalibrated_inputs_never_get_fake_outcomes(tmp_path):
    near, far = snapshot(tmp_path / "near.csv"), snapshot(tmp_path / "far.csv")
    for path in (near, far):
        path.write_text(path.read_text().replace(" 09:", " 12:"))
    output = tmp_path / "output"
    replay.run_study(near, far, start_date="2026-07-06", end_date="2026-07-06", output_dir=output)
    a = report.load_arm(list(output.glob("baseline_*.jsonl")), "baseline")
    b = report.load_arm(list(output.glob("adaptive_*.jsonl")), "adaptive")
    assert all(r["gross_pnl_points"] is None for r in a.values())
    assert all(r["signals"]["vol_zscore"] is None for r in b.values())
    assert report.compare(a, b, initial_equity=40000)["paired_count"] == 0


def test_drawdown_uses_peak_equity_and_gross_point_units(tmp_path):
    _, a, b = study(tmp_path, future_close=80)
    result = report.compare(a, b, initial_equity=40000)
    assert result["baseline"]["gross_pnl_points"] == -40
    assert result["baseline"]["max_drawdown_pct"] == pytest.approx(-0.1)
    assert result["baseline"]["worst_trade_pct_initial_equity"] == pytest.approx(-0.1)


def test_invalid_and_conflicting_source_rejected(tmp_path):
    path = snapshot(tmp_path / "near.csv")
    first = path.read_text().splitlines()[1]
    path.write_text(path.read_text() + first.replace(",100,100,100,100,93,0", ",100,101,100,101,93,0") + "\n")
    with pytest.raises(ValueError, match="conflicting"):
        replay.audit_source(path)
    path.write_text("ts,Open,High,Low,Close,Volume\n2026-07-06 09:10:00,100,90,80,100,1\n")
    with pytest.raises(ValueError, match="invalid"):
        replay.audit_source(path)


@pytest.mark.parametrize("price,ref,expected", [(110, 100, 1), (90, 100, -1), (100, 100, 0), (100, None, 0)])
def test_direction(price, ref, expected):
    assert replay.direction(price, ref) == expected
