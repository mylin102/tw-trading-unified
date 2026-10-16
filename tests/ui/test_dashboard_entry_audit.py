"""P1-C dashboard follow-up: the MTS entry-z display must render the ACTUAL
latest ENTRY_AUDIT z (spread_z + threshold + source + time) — never a
0.00 fallback from the state mirror (whose current_spread_z is never
updated)."""
import json

from ui.dashboard import latest_entry_audit_line


def _events(*audits):
    return [{"event": "LEG_FILLED", "ts": "2026-08-18T09:00:00+08:00",
             "leg": "NEAR"},
            *audits]


def _audit(z, entry_z=1.0, reason="TMF_SPREAD_WIDE", ts="2026-08-18T09:01:00+08:00"):
    return {"event": "ENTRY_AUDIT", "ts": ts, "spread_z": z,
            "entry_z": entry_z, "reason": reason, "action": "SELL_NEAR_BUY_FAR"}


def _write(tmp_path, events):
    p = tmp_path / "mts_spread_events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                 encoding="utf-8")
    return str(p)


def test_latest_entry_audit_renders_actual_z_source_and_time(tmp_path):
    path = _write(tmp_path, _events(_audit(z=3.21)))
    line = latest_entry_audit_line(path)
    assert line is not None
    assert "3.21" in line                    # actual z, not 0.00
    assert "1.00" in line                    # threshold
    assert "TMF_SPREAD_WIDE" in line         # source
    assert "2026-08-18 09:01:00" in line     # time


def test_latest_entry_audit_picks_the_newest(tmp_path):
    path = _write(tmp_path, _events(
        _audit(z=2.50, ts="2026-08-18T09:01:00+08:00"),
        _audit(z=3.75, ts="2026-08-18T09:05:00+08:00")))
    line = latest_entry_audit_line(path)
    assert "3.75" in line
    assert "2.50" not in line


def test_no_entry_audit_returns_none(tmp_path):
    path = _write(tmp_path, _events())
    assert latest_entry_audit_line(path) is None


def test_missing_z_renders_na_not_zero(tmp_path):
    path = _write(tmp_path, _events(_audit(z=None)))
    line = latest_entry_audit_line(path)
    assert line is not None
    assert "N/A" in line
    assert "0.00" not in line


def test_missing_events_file_returns_none(tmp_path):
    assert latest_entry_audit_line(str(tmp_path / "nope.jsonl")) is None
