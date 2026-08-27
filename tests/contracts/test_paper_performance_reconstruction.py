"""
Contract: MTS paper daily performance reconstruction.

codex spec (2026-08-27) scenarios:
  1. paper runtime + mixed historical ledger -> reconstructed metrics
     displayable, formal totals EXCLUDE them
  2. missing entry/exit/terminal -> excluded with reason
  3. explicit live record -> never classified paper
  4. qty/side/contract/trade_id mismatch -> excluded
  5. settlement_origin=LIVE legacy anomaly -> unverified, original data
     untouched
  6. full paper provenance -> VERIFIED_PAPER
  7. runtime context canonical path; repo/runtime mismatch telemetry
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.paper_performance_reconstruction import (
    reconstruct_paper_daily,
)


def _fill(trade_id, fill_type, leg=None, realized=None, **extra):
    r = {
        "trade_id": trade_id,
        "fill_type": fill_type,
        "leg": leg,
        "realized_pnl": realized,
        "timestamp": extra.pop("timestamp", "2026-08-27T09:00:00"),
        "session": extra.pop("session", "day"),
        "qty": extra.pop("qty", 1),
        "price": extra.pop("price", 100.0),
        "side": extra.pop("side", "SHORT" if fill_type == "ENTRY" else "BUY"),
        "contract": extra.pop("contract", "TMFI6" if leg == "NEAR" else "TMFJ6"),
    }
    r.update(extra)
    return r


def _combined_settled(tid, net, gross, **extra):
    r = {
        "trade_id": tid,
        "fill_type": "COMBINED_EXIT_SETTLED",
        "combined_realized_pnl_net": net,
        "combined_realized_pnl_gross": gross,
        "near_closed_qty": 1,
        "far_closed_qty": 1,
        "timestamp": "2026-08-27T10:00:00",
        "session": "day",
    }
    r.update(extra)
    return r


def _write_ledgers(tmp_path, fills):
    os.makedirs(tmp_path, exist_ok=True)
    fp = tmp_path / "mts_trade_fills.jsonl"
    with open(fp, "w") as f:
        for rec in fills:
            f.write(json.dumps(rec) + "\n")
    ep = tmp_path / "mts_spread_events.jsonl"
    ep.write_text("")
    return str(fp), str(ep)


def _complete_trade(tid, **over):
    """A complete combined-exit trade: near+far entry, two leg exits,
    settlement."""
    return [
        _fill(tid, "ENTRY", "NEAR", side="SHORT", price=100.0, **over),
        _fill(tid, "ENTRY", "FAR", side="LONG", price=101.0, **over),
        _fill(tid, "COMBINED_EXIT", "NEAR", realized=50.0, price=99.0,
              side="BUY"),
        _fill(tid, "COMBINED_EXIT", "FAR", realized=-20.0, price=102.0,
              side="SELL"),
        _combined_settled(tid, net=30.0, gross=30.0),
    ]


class TestReconstruction:
    def test_legacy_complete_trade_reconstructed_and_formal_excluded(self):
        """paper runtime + legacy complete ledger -> RECONSTRUCTED metrics
        displayable; formal totals (VERIFIED_PAPER only) stay empty."""
        fills = _complete_trade("t1")
        fp, ep = _write_ledgers(Path("/tmp/ppr_t1"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert len(res["eligible"]["RECONSTRUCTED"]) == 1
        assert res["eligible"]["RECONSTRUCTED"][0]["provenance_badge"] == \
            "RECONSTRUCTED_FROM_PAPER_STATE"
        assert res["metrics"]["completed_trades"] == 1
        assert res["metrics"]["realized_gross"] == 30.0
        # formal totals exclude reconstructed
        assert res["formal_metrics"]["completed_trades"] == 0

    def test_missing_exit_excluded(self):
        """Entry only, no exit/terminal -> excluded with reason."""
        fills = [
            _fill("t2", "ENTRY", "NEAR"),
            _fill("t2", "ENTRY", "FAR"),
        ]
        fp, ep = _write_ledgers(Path("/tmp/ppr_t2"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert res["excluded"] and res["excluded"][0]["reason"] == \
            "missing_exit_or_terminal"
        assert res["metrics"]["completed_trades"] == 0

    def test_explicit_live_never_paper(self):
        """mode=live record -> excluded, never classified paper."""
        fills = _complete_trade("t3", mode="live")
        fp, ep = _write_ledgers(Path("/tmp/ppr_t3"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert any(e["reason"] == "mode=live" for e in res["excluded"])
        assert res["metrics"]["completed_trades"] == 0

    def test_qty_mismatch_excluded(self):
        """near qty != far qty -> excluded."""
        fills = _complete_trade("t4")
        fills[0]["qty"] = 2  # near=2, far=1
        fp, ep = _write_ledgers(Path("/tmp/ppr_t4"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert any("qty_mismatch" in e["reason"] for e in res["excluded"])

    def test_settlement_origin_live_marks_unverified(self):
        """Legacy settlement_origin=LIVE anomaly -> UNVERIFIED badge,
        original data untouched."""
        fills = _complete_trade("t5")
        fills[-1]["settlement_origin"] = "LIVE"
        fp, ep = _write_ledgers(Path("/tmp/ppr_t5"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert len(res["eligible"]["UNVERIFIED"]) == 1
        assert res["eligible"]["UNVERIFIED"][0]["unverified_reason"] == \
            "settlement_origin=LIVE"
        # original ledger untouched
        with open(fp) as f:
            assert "settlement_origin" in f.read()

    def test_full_provenance_verified_paper(self):
        """Complete paper provenance -> VERIFIED_PAPER + formal totals."""
        fills = _complete_trade("t6")
        for f in fills:
            f["mode"] = "paper"
            f["paper_fill_id"] = f"PF-{f['fill_type']}-{f.get('leg')}"
            f["execution_source"] = (
                "PAPER_COMBO_FILL" if "EXIT" not in f["fill_type"]
                else "PAPER_EXIT_FILL")
            f["parent_combo_order_id"] = "COMBO-6"
            f["leg_role"] = f.get("leg") or "NEAR"
            f["expected_qty"] = 1
            f["filled_qty"] = 1
            f["fill_status"] = "FILLED"
            f["fill_price"] = f.get("price") or f.get("combined_realized_pnl_gross") or 0.0
            f["fill_timestamp"] = f["timestamp"]
            f["attribution_key"] = f"{f['trade_id']}:{f.get('leg')}"
            f["session_id"] = "S6"
            f["generation_id"] = "G6"
            f["release_group_id"] = "RG6"
        fp, ep = _write_ledgers(Path("/tmp/ppr_t6"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert len(res["eligible"]["VERIFIED_PAPER"]) == 1
        assert res["formal_metrics"]["completed_trades"] == 1
        assert res["formal_metrics"]["realized_gross"] == 30.0

    def test_wrong_trading_day_excluded(self):
        """Trade on a different trading day -> not in target day."""
        fills = _complete_trade("t7", timestamp="2026-08-26T09:00:00")
        fp, ep = _write_ledgers(Path("/tmp/ppr_t7"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert res["metrics"]["completed_trades"] == 0
        assert res["eligible"]["RECONSTRUCTED"] == []

    def test_metrics_winrate_and_profit_factor(self):
        """Metrics: win rate + profit factor across reconstructed."""
        fills = (_complete_trade("w1") + _complete_trade("w2")
                 + _complete_trade("l1"))
        # make l1 a loser: flip settlement net to negative
        fills[-1]["combined_realized_pnl_net"] = -30.0
        fills[-1]["combined_realized_pnl_gross"] = -30.0
        fp, ep = _write_ledgers(Path("/tmp/ppr_m"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        m = res["metrics"]
        assert m["completed_trades"] == 3
        assert m["realized_gross"] == 30.0  # 30+30-30
        assert abs(m["win_rate"] - 2 / 3) < 1e-9
        assert abs(m["profit_factor"] - 60.0 / 30.0) < 1e-9

    def test_later_fill_mode_live_excludes_whole_trade(self):
        """mode=live on a LATER fill (not the entry) -> whole trade
        excluded (codex review #1)."""
        fills = _complete_trade("t8")
        fills[3]["mode"] = "live"  # the FAR combined-exit fill
        fp, ep = _write_ledgers(Path("/tmp/ppr_t8"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert any(e["reason"] == "mode=live" for e in res["excluded"])
        assert res["metrics"]["completed_trades"] == 0

    def test_price_source_broker_fill_marks_unverified(self):
        """price_source=BROKER_FILL on a fill -> UNVERIFIED anomaly
        (codex review #2)."""
        fills = _complete_trade("t9")
        fills[2]["price_source"] = "BROKER_FILL"
        fp, ep = _write_ledgers(Path("/tmp/ppr_t9"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert len(res["eligible"]["UNVERIFIED"]) == 1
        assert res["eligible"]["UNVERIFIED"][0]["unverified_reason"] == \
            "price_source=BROKER_FILL"

    def test_exit_side_mismatch_excluded(self):
        """Exit side not closing the entry position -> excluded (codex
        review #3)."""
        fills = _complete_trade("t10")
        # NEAR entry SHORT must close with BUY; flip it to SELL
        fills[2]["side"] = "SELL"
        fp, ep = _write_ledgers(Path("/tmp/ppr_t10"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert any("side_mismatch" in e["reason"] for e in res["excluded"])

    def test_release_path_legs_covered(self):
        """Single-leg release path (RELEASE one leg + EXIT remaining) must
        pass pairing (codex review: RELEASE fills count as exit evidence)."""
        fills = [
            _fill("r1", "ENTRY", "NEAR", side="LONG", price=100.0),
            _fill("r1", "ENTRY", "FAR", side="SHORT", price=101.0),
            _fill("r1", "RELEASE", "FAR", realized=131.4, price=99.0,
                  side="BUY"),
            _fill("r1", "EXIT", "NEAR", realized=271.5, price=102.0,
                  side="SELL"),
        ]
        fp, ep = _write_ledgers(Path("/tmp/ppr_r1"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert len(res["eligible"]["RECONSTRUCTED"]) == 1, res["excluded"]
        assert res["eligible"]["RECONSTRUCTED"][0]["net_pnl"] == \
            131.4 + 271.5

    def test_terminal_closed_qty_missing_excluded(self):
        """Terminal present but near/far closed qty MISSING -> excluded
        (codex review: missing/zero terminal qty must fail closed)."""
        fills = _complete_trade("t11")
        fills[-1].pop("near_closed_qty")
        fp, ep = _write_ledgers(Path("/tmp/ppr_t11"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert any("terminal_near_closed_qty_missing" in e["reason"]
                   for e in res["excluded"])

    def test_terminal_closed_qty_zero_excluded(self):
        """Terminal with near_closed_qty=0 -> excluded."""
        fills = _complete_trade("t12")
        fills[-1]["near_closed_qty"] = 0
        fp, ep = _write_ledgers(Path("/tmp/ppr_t12"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert any("terminal_near_closed_qty_missing" in e["reason"]
                   for e in res["excluded"])

    def test_exit_overfill_excluded(self):
        """Exit qty EXCEEDS entry qty -> excluded (codex review: >= would
        silently accept overfill; must be strictly equal)."""
        fills = _complete_trade("t13")
        fills[2]["qty"] = 2  # near exit qty=2 vs entry qty=1 -> overfill
        fp, ep = _write_ledgers(Path("/tmp/ppr_t13"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert any("exit_qty_mismatch" in e["reason"] for e in res["excluded"])

    def test_unknown_entry_side_excluded(self):
        """Entry side unidentifiable -> excluded (fail-closed, no skip)."""
        fills = _complete_trade("t14")
        fills[0]["side"] = "WEIRD"  # unknown entry side
        fp, ep = _write_ledgers(Path("/tmp/ppr_t14"), fills)
        res = reconstruct_paper_daily(fp, ep, "2026-08-27")
        assert any("entry_side_unidentifiable" in e["reason"]
                   for e in res["excluded"])
