"""RED/GREEN: dashboard manual-command status freshness gate.

The dashboard showed a 09:00:09 FAILED cmd-close for hours because the
status record (/tmp/futures_manual_trade_status.json) is displayed
unconditionally.  Stale statuses must not keep displaying past
failures.
"""

import datetime


def test_old_command_status_not_fresh():
    from ui.dashboard import _manual_command_is_fresh

    assert _manual_command_is_fresh(
        {"ts": "2026-08-17T09:00:09", "status": "FAILED"}) is False


def test_recent_command_status_fresh():
    from ui.dashboard import _manual_command_is_fresh

    now = datetime.datetime.now().isoformat()
    assert _manual_command_is_fresh(
        {"ts": now, "status": "COMPLETED"}) is True


def test_missing_ts_not_fresh():
    from ui.dashboard import _manual_command_is_fresh

    assert _manual_command_is_fresh({"status": "FAILED"}) is False
    assert _manual_command_is_fresh({}) is False


def test_bad_ts_not_fresh():
    from ui.dashboard import _manual_command_is_fresh

    assert _manual_command_is_fresh({"ts": "garbage"}) is False


def test_none_not_fresh():
    from ui.dashboard import _manual_command_is_fresh

    assert _manual_command_is_fresh(None) is False
