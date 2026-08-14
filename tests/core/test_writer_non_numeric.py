"""Round-4 edge gap: the writer must NOT raise on a non-numeric pnl —
the whole artifact write would be skipped and a stale (<60s) artifact
would keep serving old UPL.  Non-numeric pnl -> None in the written
artifact (reader then renders N/A), artifact still written fresh.
"""
import json
from types import SimpleNamespace

import core.runtime_paths as rp
from tests.core.test_fills_recovery_live_gate import _AutoMonitor


def test_writer_non_numeric_pnl_writes_none(tmp_path, monkeypatch):
    mon = _AutoMonitor.__new__(_AutoMonitor)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    ctx = SimpleNamespace(session_id="sess-1")
    mon._write_live_session_upl(
        [{"code": "TMFH6", "direction": "S", "quantity": 1,
          "avg_cost": 45962.0, "pnl": "not-a-number"}], ctx)
    p = tmp_path / "live_session_upl.json"
    assert p.exists()  # artifact must be written — no stale window
    d = json.loads(p.read_text())
    assert d["legs"]["TMFH6"]["pnl"] is None  # non-numeric -> None (N/A)


def test_writer_non_numeric_pnl_with_valid_far_leg(tmp_path, monkeypatch):
    """A non-numeric near-leg pnl must not drop the valid far leg either."""
    mon = _AutoMonitor.__new__(_AutoMonitor)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    ctx = SimpleNamespace(session_id="sess-1")
    mon._write_live_session_upl(
        [{"code": "TMFH6", "direction": "S", "quantity": 1,
          "avg_cost": 45962.0, "pnl": "abc"},
         {"code": "TMFI6", "direction": "B", "quantity": 1,
          "avg_cost": 46088.0, "pnl": -680.0}], ctx)
    d = json.loads((tmp_path / "live_session_upl.json").read_text())
    assert d["legs"]["TMFH6"]["pnl"] is None
    assert d["legs"]["TMFI6"]["pnl"] == -680.0


def test_writer_non_numeric_avg_cost_writes_none(tmp_path, monkeypatch):
    """Same class: non-numeric avg_cost must not raise either."""
    mon = _AutoMonitor.__new__(_AutoMonitor)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    ctx = SimpleNamespace(session_id="sess-1")
    mon._write_live_session_upl(
        [{"code": "TMFH6", "direction": "S", "quantity": 1,
          "avg_cost": "oops", "pnl": 650.0}], ctx)
    p = tmp_path / "live_session_upl.json"
    assert p.exists()
    d = json.loads(p.read_text())
    assert d["legs"]["TMFH6"]["avg_cost"] is None
