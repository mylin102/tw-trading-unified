import json
import sqlite3


def _audit():
    return {
        "event_time": "2026-08-14T09:00:00.000000",
        "near_contract": "TMFH6",
        "far_contract": "TMFI6",
        "spread": -155.0,
        "spread_z": 3.42,
        "entry_z": 3.0,
        "spread_std": 41.8,
        "atr": 51.2,
        "action": "SELL_NEAR_BUY_FAR",
        "expected_reversion": "SPREAD_TO_NARROW",
        "near_bid": 46410.0,
        "near_ask": 46411.0,
        "far_bid": 46565.0,
        "far_ask": 46566.0,
    }


def test_entry_research_store_schema_and_provenance(tmp_path, monkeypatch):
    from core.entry_research_store import record_entry_observation

    path = tmp_path / "mts_entry_research.sqlite3"
    assert record_entry_observation(
        _audit(), mode="paper", session_id="session-1",
        config_hash="config-1", release_sha="release-1", run_id="run-1",
        db_path=path
    ) is True

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT mode, session_id, config_hash, release_sha, run_id, "
        "near_contract, far_contract, spread_z, entry_z_threshold, "
        "near_bid, far_ask, candidate_direction, decision "
        "FROM entry_observations"
    ).fetchone()
    conn.close()
    assert row == (
        "paper", "session-1", "config-1", "release-1", "run-1",
        "TMFH6", "TMFI6", 3.42, 3.0, 46410.0, 46566.0,
        "SELL_NEAR_BUY_FAR", "ENTER"
    )


def test_entry_research_store_duplicate_is_idempotent(tmp_path):
    from core.entry_research_store import record_entry_observation

    path = tmp_path / "mts_entry_research.sqlite3"
    assert record_entry_observation(_audit(), mode="paper", db_path=path)
    assert record_entry_observation(_audit(), mode="paper", db_path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM entry_observations").fetchone()[0] == 1
    conn.close()


def test_candidate_and_enter_observations_are_distinct(tmp_path):
    from core.entry_research_store import record_entry_observation

    path = tmp_path / "mts_entry_research.sqlite3"
    candidate = _audit() | {
        "decision": "CANDIDATE",
        "rejection_reason": "CANDIDATE_AWAITING_EVALUATION",
    }
    entered = _audit() | {"decision": "ENTER"}
    assert record_entry_observation(candidate, mode="paper", db_path=path)
    assert record_entry_observation(entered, mode="paper", db_path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM entry_observations").fetchone()[0] == 2
    assert {row[0] for row in conn.execute("SELECT decision FROM entry_observations")} == {
        "CANDIDATE", "ENTER"
    }
    conn.close()


def test_entry_research_store_failure_is_non_blocking(monkeypatch, tmp_path):
    import core.entry_research_store as store

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(store.sqlite3, "connect", fail_connect)
    assert store.record_entry_observation(_audit(), db_path=tmp_path / "blocked.db") is False
