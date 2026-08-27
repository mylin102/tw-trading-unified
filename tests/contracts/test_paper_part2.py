"""
Part 2 tests: future paper-fill contract + dashboard reconstructed block.

codex spec §3: new paper fills carry full provenance
  (mode=paper, execution_source=PAPER_*, paper_fill_id, leg_role,
   expected/filled_qty, fill_status, fill_price, fill_timestamp,
   attribution_key, session_id/generation_id, release_group_id)
  and NEVER use settlement_origin=LIVE / BROKER_FILL for paper fills.

codex spec §2: dashboard shows a SEPARATE reconstructed block with
  badges (PAPER · RECONSTRUCTED · UNVERIFIED); formal totals untouched.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the MTS fill log at a tmp file per test.  _MTS_FILL_LOG is
    snapshotted at tmf_spread import, so patch the module attribute too."""
    _log = tmp_path / "mts_trade_fills.jsonl"
    monkeypatch.setenv("MTS_FILL_LOG_PATH", str(_log))
    monkeypatch.setenv("MTS_BACKTEST", "0")
    try:
        import strategies.plugins.futures.active.tmf_spread as _t
        monkeypatch.setattr(_t, "_MTS_FILL_LOG", str(_log))
    except Exception:
        pass
    yield _log


class TestPaperFillContract:
    def test_append_fill_carries_full_provenance(self, _isolate):
        """A new paper fill must carry every provenance field."""
        from strategies.plugins.futures.active.tmf_spread import _append_fill
        _append_fill(
            "TMF", "TMFI6", "NEAR", "SELL", 1, 100.0, "ENTRY", "t-p1",
            price_source="PAPER_FILL",
            session_id="SESS-1", generation_id="GEN-1",
            parent_combo_order_id="COMBO-1",
            release_group_id="RG-1",
        )
        rec = json.loads(_isolate.read_text().strip().splitlines()[-1])
        assert rec["mode"] == "paper"
        assert rec["execution_source"] == "PAPER_COMBO_FILL"
        assert rec["paper_fill_id"].startswith("PF-")
        assert rec["leg_role"] == "NEAR"
        assert rec["expected_qty"] == 1
        assert rec["filled_qty"] == 1
        assert rec["fill_status"] == "FILLED"
        assert rec["fill_price"] == 100.0
        assert rec["attribution_key"] == "t-p1:NEAR:ENTRY"
        assert rec["session_id"] == "SESS-1"
        assert rec["generation_id"] == "GEN-1"
        assert rec["release_group_id"] == "RG-1"

    def test_exit_fill_source_paper_exit_fill(self, _isolate):
        from strategies.plugins.futures.active.tmf_spread import _append_fill
        _append_fill("TMF", "TMFI6", "NEAR", "BUY", 1, 99.0,
                     "COMBINED_EXIT", "t-p2", realized_pnl=50.0,
                     price_source="PAPER_FILL")
        rec = json.loads(_isolate.read_text().strip().splitlines()[-1])
        assert rec["execution_source"] == "PAPER_EXIT_FILL"
        assert rec["mode"] == "paper"

    def test_paper_fill_never_broker_fill_source(self, _isolate):
        """The COMBINED_EXIT settlement writer in paper mode must use
        PAPER_FILL, not BROKER_FILL (monitor.py _px_source fix)."""
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "strategies/futures/monitor.py")).read()
        # paper branch produces PAPER_FILL; BROKER_FILL only when live
        assert '"PAPER_FILL" if _exit_px is not None' in src
        assert '"BROKER_FILL" if (self.live_trading' in src


class TestDashboardBlock:
    def test_dashboard_has_reconstructed_block(self):
        """The dashboard must contain the reconstructed-paper block with
        badges and the formal-exclusion caption."""
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "ui/dashboard.py")).read()
        assert "MTS Paper 重建績效" in src
        assert "不計入上方正式累計" in src
        assert "reconstruct_paper_daily" in src
        assert "PAPER · VERIFIED" in src

    def test_reconstructed_block_not_in_formal_metrics(self):
        """The reconstructed block reads from reconstruct_paper_daily's
        metrics (which exclude VERIFIED-only formal totals)."""
        from core.paper_performance_reconstruction import (
            reconstruct_paper_daily)
        # a legacy trade -> RECONSTRUCTED, formal stays 0
        import json as _json
        _d = Path("/tmp/ppr_dash")
        _d.mkdir(exist_ok=True)
        _fp = _d / "fills.jsonl"
        with open(_fp, "w") as f:
            for rec in [
                {"trade_id": "d1", "fill_type": "ENTRY", "leg": "NEAR",
                 "side": "SHORT", "qty": 1, "price": 100.0,
                 "timestamp": "2026-08-27T09:00:00", "session": "day"},
                {"trade_id": "d1", "fill_type": "ENTRY", "leg": "FAR",
                 "side": "LONG", "qty": 1, "price": 101.0,
                 "timestamp": "2026-08-27T09:00:01", "session": "day"},
                {"trade_id": "d1", "fill_type": "COMBINED_EXIT", "leg": "NEAR",
                 "side": "BUY", "qty": 1, "price": 99.0, "realized_pnl": 50.0,
                 "timestamp": "2026-08-27T10:00:00", "session": "day"},
                {"trade_id": "d1", "fill_type": "COMBINED_EXIT", "leg": "FAR",
                 "side": "SELL", "qty": 1, "price": 102.0,
                 "realized_pnl": -20.0,
                 "timestamp": "2026-08-27T10:00:01", "session": "day"},
                {"trade_id": "d1", "fill_type": "COMBINED_EXIT_SETTLED",
                 "combined_realized_pnl_net": 30.0,
                 "combined_realized_pnl_gross": 30.0,
                 "near_closed_qty": 1, "far_closed_qty": 1,
                 "timestamp": "2026-08-27T10:00:02", "session": "day"},
            ]:
                f.write(_json.dumps(rec) + "\n")
        _ep = _d / "events.jsonl"
        _ep.write_text("")
        res = reconstruct_paper_daily(str(_fp), str(_ep), "2026-08-27")
        # metrics (reconstructed block) include it; formal excludes it
        assert res["metrics"]["completed_trades"] == 1
        assert res["formal_metrics"]["completed_trades"] == 0
