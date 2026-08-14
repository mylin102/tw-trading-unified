import json
import sqlite3


def _audit():
    return {
        "event_time": "2026-08-14T09:00:00.000000",
        "near_contract": "TMFH6", "far_contract": "TMFI6",
        "spread": -155.0, "spread_z": 3.42, "entry_z": 3.0,
        "dz": -0.2, "spread_slope": -0.1, "velocity_ema": -0.05,
        "near_bid": 46410.0, "near_ask": 46411.0,
        "far_bid": 46565.0, "far_ask": 46566.0,
        "action": "SELL_NEAR_BUY_FAR", "expected_net_edge": 20.0,
    }


def test_report_no_database(tmp_path):
    from scripts.research.generate_entry_report import _read_db, write_report
    report = _read_db(tmp_path / "missing.sqlite3")
    assert report["status"] == "NO_DATABASE"
    out = tmp_path / "report.json"
    write_report(report, out)
    assert json.loads(out.read_text())["status"] == "NO_DATABASE"


def test_report_provenance_and_mode_separation(tmp_path):
    from core.entry_research_store import record_entry_observation
    from scripts.research.generate_entry_report import _read_db
    db = tmp_path / "research.sqlite3"
    assert record_entry_observation(_audit() | {"decision": "CANDIDATE"}, mode="paper", source="paper_strategy", db_path=db)
    assert record_entry_observation(_audit() | {"decision": "ENTER", "event_time": "2026-08-14T09:01:00"}, mode="live", source="live_strategy", session_id="s", config_hash="c", release_sha="r", run_id="run", db_path=db)
    report = _read_db(db)
    assert report["status"] == "READY_FOR_RESEARCH"
    assert report["modes"] == {"live": 1, "paper": 1}
    assert report["decisions"] == {"CANDIDATE": 1, "ENTER": 1}


def test_report_flags_source_mismatch(tmp_path):
    from core.entry_research_store import record_entry_observation
    from scripts.research.generate_entry_report import _read_db
    db = tmp_path / "research.sqlite3"
    assert record_entry_observation(_audit(), mode="live", source="paper_strategy", db_path=db)
    report = _read_db(db)
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["source_mismatch_rows"] == 1


def test_monitor_records_threshold_candidate_before_strategy_result(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime
    from strategies.futures.monitor import FuturesMonitor

    monkeypatch.setenv("MTS_ENTRY_RESEARCH_DB", str(tmp_path / "research.sqlite3"))
    monitor = object.__new__(FuturesMonitor)
    monitor.live_trading = False
    monitor.near_code = "TMFH6"
    monitor.far_code = "TMFI6"
    monitor._execution_context = SimpleNamespace(session_id="paper-session", config_hash="paper-config")
    strategy = SimpleNamespace(_entry_z=3.0, _trade_id=None)
    monitor._record_mts_entry_research_candidate(strategy, {
        "spread": 160.0, "spread_z": -3.4, "near_bid": 46410.0,
        "near_ask": 46411.0, "far_bid": 46565.0, "far_ask": 46566.0,
    }, datetime(2026, 8, 14, 9, 0))
    conn = sqlite3.connect(tmp_path / "research.sqlite3")
    row = conn.execute("SELECT decision, candidate_direction, mode FROM entry_observations").fetchone()
    conn.close()
    assert row == ("CANDIDATE", "BUY_NEAR_SELL_FAR", "paper")
