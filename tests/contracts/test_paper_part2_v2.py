"""
Codex review v2 tests: formal KPI exclusion + mode authority.

  - formal KPI must read formal_metrics (VERIFIED_PAPER only), never
    parse_logs() totals (which mix reconstructed trades)
  - reconstructed metrics only in the separate block
  - paper writer mode decided by runtime authority, not arbitrary env
  - missing required provenance -> not VERIFIED_PAPER
  - mixed ledger (mode=live present) still shows rebuildable paper trades
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


class TestFormalKpiExclusion:
    def test_formal_kpi_uses_formal_metrics_not_parse_logs(self):
        """The dashboard formal-KPI block must read formal_metrics, not
        parse_logs totals."""
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "ui/dashboard.py")).read()
        # formal KPI renders from formal_metrics
        assert '_formal_metrics.get("completed_trades")' in src
        assert "_formal_metrics.get('realized_gross', 0)" in src
        # it must NOT use _total_net/_total_trades (parse_logs-derived)
        # inside the formal branch — those stay for the breakdown table
        assert "尚無 VERIFIED_PAPER provenance" in src

    def test_parse_logs_17_never_formal(self):
        """parse_logs() on 8/27 yields totals INCLUDING an open trade;
        formal_metrics (VERIFIED_PAPER) must be 0 — the two must never
        be conflated.  (Counts are dynamic — the live ledger gains new
        trades; the invariant is formal==0 and metrics exclude the open
        trade.)"""
        from core.paper_performance_reconstruction import (
            reconstruct_paper_daily)
        F = "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime/logs/mts_trade_fills.jsonl"
        E = "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime/logs/mts_spread_events.jsonl"
        res = reconstruct_paper_daily(F, E, "2026-08-27")
        # metrics (reconstructed block) exclude the open trade
        assert res["metrics"]["completed_trades"] >= 16
        # formal (VERIFIED_PAPER) = 0 — parse_logs's totals must not
        # leak into formal
        assert res["formal_metrics"]["completed_trades"] == 0
        assert res["formal_metrics"]["realized_gross"] == 0
        # the open trade (missing terminal) is excluded
        assert any(e["reason"] == "missing_exit_or_terminal"
                   for e in res["excluded"])


class TestModeAuthority:
    def _patch_ctx(self, monkeypatch, effective_mode):
        """Patch core.execution_context_state.read_execution_context (the
        module _authorized_fill_mode imports INSIDE the function)."""
        import core.execution_context_state as _ecs
        monkeypatch.setattr(
            _ecs, "read_execution_context",
            lambda *a, **k: {"effective_mode": effective_mode})

    def test_fill_mode_from_runtime_authority(self, monkeypatch):
        """_authorized_fill_mode must read the canonical execution context
        (paper_active -> paper), never an env override."""
        from strategies.plugins.futures.active import tmf_spread as _t
        self._patch_ctx(monkeypatch, "paper_active")
        monkeypatch.setenv("MTS_EXECUTION_MODE", "live")  # hostile override
        assert _t._authorized_fill_mode() == "paper"

    def test_fill_mode_live_context(self, monkeypatch):
        from strategies.plugins.futures.active import tmf_spread as _t
        self._patch_ctx(monkeypatch, "live_ready")
        assert _t._authorized_fill_mode() == "live"

    def test_fill_mode_unknown_fails_closed(self, monkeypatch):
        from strategies.plugins.futures.active import tmf_spread as _t
        self._patch_ctx(monkeypatch, "weird")
        assert _t._authorized_fill_mode() == "unknown"

    def test_provenance_requires_all_fields(self):
        """_has_full_provenance must require ALL provenance fields valid
        (None session_id/generation_id etc. -> not VERIFIED_PAPER)."""
        from core.paper_performance_reconstruction import _has_full_provenance
        _base = {
            "mode": "paper",
            "paper_fill_id": "PF-1",
            "execution_source": "PAPER_COMBO_FILL",
            "parent_combo_order_id": "C1",
            "leg_role": "NEAR",
            "expected_qty": 1,
            "filled_qty": 1,
            "fill_status": "FILLED",
            "fill_price": 100.0,
            "fill_timestamp": "2026-08-27T09:00:00",
            "attribution_key": "K1",
            "session_id": "S1",
            "generation_id": "G1",
            "release_group_id": "R1",
        }
        fills = [dict(_base)]
        assert _has_full_provenance(fills) is True
        # missing session_id -> False (must not be VERIFIED_PAPER)
        fills[0]["session_id"] = None
        assert _has_full_provenance(fills) is False
        # missing parent_combo_order_id -> False
        fills[0] = dict(_base)
        fills[0]["parent_combo_order_id"] = None
        assert _has_full_provenance(fills) is False


class TestMixedLedger:
    def test_mixed_ledger_still_shows_paper_trades(self):
        """A ledger with one mode=live record must exclude that trade but
        still display safely rebuildable paper/legacy trades."""
        from core.paper_performance_reconstruction import (
            reconstruct_paper_daily)
        import json as _json
        _d = Path("/tmp/ppr_mixed")
        _d.mkdir(exist_ok=True)
        _fp = _d / "fills.jsonl"
        rows = [
            # paper/legacy trade (complete)
            {"trade_id": "p1", "fill_type": "ENTRY", "leg": "NEAR",
             "side": "SHORT", "qty": 1, "price": 100.0,
             "timestamp": "2026-08-27T09:00:00", "session": "day"},
            {"trade_id": "p1", "fill_type": "ENTRY", "leg": "FAR",
             "side": "LONG", "qty": 1, "price": 101.0,
             "timestamp": "2026-08-27T09:00:01", "session": "day"},
            {"trade_id": "p1", "fill_type": "COMBINED_EXIT", "leg": "NEAR",
             "side": "BUY", "qty": 1, "price": 99.0, "realized_pnl": 50.0,
             "timestamp": "2026-08-27T10:00:00", "session": "day"},
            {"trade_id": "p1", "fill_type": "COMBINED_EXIT", "leg": "FAR",
             "side": "SELL", "qty": 1, "price": 102.0,
             "realized_pnl": -20.0,
             "timestamp": "2026-08-27T10:00:01", "session": "day"},
            {"trade_id": "p1", "fill_type": "COMBINED_EXIT_SETTLED",
             "combined_realized_pnl_net": 30.0,
             "combined_realized_pnl_gross": 30.0,
             "near_closed_qty": 1, "far_closed_qty": 1,
             "timestamp": "2026-08-27T10:00:02", "session": "day"},
            # explicit live trade (must be EXCLUDED, not poison the scope)
            {"trade_id": "l1", "mode": "live", "fill_type": "ENTRY",
             "leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0,
             "timestamp": "2026-08-27T09:30:00", "session": "day"},
        ]
        with open(_fp, "w") as f:
            for r in rows:
                f.write(_json.dumps(r) + "\n")
        _ep = _d / "events.jsonl"
        _ep.write_text("")
        res = reconstruct_paper_daily(str(_fp), str(_ep), "2026-08-27")
        # the paper trade is displayed; the live trade excluded
        assert len(res["eligible"]["RECONSTRUCTED"]) == 1
        assert any(e["reason"] == "mode=live" for e in res["excluded"])
        assert res["metrics"]["completed_trades"] == 1
