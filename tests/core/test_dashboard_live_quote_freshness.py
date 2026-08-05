import json
from datetime import datetime, timedelta

from core.dashboard_data import read_mts_quote_freshness


def _write_state(path, *, heartbeat_at, quote_age_ms=0):
    path.write_text(json.dumps({
        "heartbeat_at": heartbeat_at.isoformat(),
        "quote_age_ms": quote_age_ms,
    }))


def test_live_quote_freshness_accepts_current_heartbeat(tmp_path):
    now = datetime(2026, 8, 5, 12, 50, 0)
    path = tmp_path / "mts_state.json"
    _write_state(path, heartbeat_at=now - timedelta(seconds=3), quote_age_ms=12)

    status = read_mts_quote_freshness(path, now=now)

    assert status["available"] is True
    assert status["fresh"] is True
    assert status["quote_age_ms"] == 12.0


def test_live_quote_freshness_rejects_stale_heartbeat(tmp_path):
    now = datetime(2026, 8, 5, 12, 50, 0)
    path = tmp_path / "mts_state.json"
    _write_state(path, heartbeat_at=now - timedelta(seconds=91), quote_age_ms=0)

    assert read_mts_quote_freshness(path, now=now)["fresh"] is False


def test_live_quote_freshness_rejects_missing_or_invalid_state(tmp_path):
    assert read_mts_quote_freshness(tmp_path / "missing.json")["available"] is False

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not-json")
    assert read_mts_quote_freshness(corrupt)["fresh"] is False
