"""
Tests for the unified MTS trade settlement pipeline (settle_mts_trade).

Covers:
  1. Normal EXIT → computes correct realized PnL
  2. Emergency close_all both legs → SETTLED with correct PnL
  3. Partial fill (one leg only) → PARTIALLY_SETTLED
  4. Duplicate fill callback → idempotent (PnL not double-counted)
  5. Missing entry price → UNRESOLVED, realized_pnl=None
  6. Fees subtracted from gross → net_realized_pnl correct
  7. SHORT side → PnL sign correct (price down = positive PnL)
  8. LONG side → PnL sign correct (price up = positive PnL)
  9. Normal EXIT vs emergency EXIT same fills → same PnL
 10. Restart replay → doesn't re-accumulate PnL
"""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from strategies.plugins.futures.active.tmf_spread import (
    SETTLEMENT_STATUS_PENDING,
    SETTLEMENT_STATUS_PARTIAL,
    SETTLEMENT_STATUS_SETTLED,
    SETTLEMENT_STATUS_UNRESOLVED,
    settle_mts_trade,
    _has_settled_exit,
    _MTS_FILL_LOG,
    _MTS_STATE_FILE,
    get_point_value,
)


# ═══════════════════════════════════════════════════════════════
# Helper fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _env_backtest_off():
    """Ensure MTS_BACKTEST is not set so _append_fill writes to log."""
    old = os.environ.pop("MTS_BACKTEST", None)
    yield
    if old is not None:
        os.environ["MTS_BACKTEST"] = old


@pytest.fixture
def tmp_fill_log():
    """Temporary fills log path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def tmp_state_file():
    """Temporary state file path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(json.dumps({"has_position": True, "state": "OPEN"}))
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)


def _read_fills(path):
    """Read all fill records from a JSONL file."""
    records = []
    if not os.path.exists(path):
        return records
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ═══════════════════════════════════════════════════════════════
# Test 1: Normal EXIT → correct PnL
# ═══════════════════════════════════════════════════════════════

class TestNormalExitPnL:
    """Normal EXIT: LONG near / SHORT far, both legs exit at profit."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_1.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_1.json")
    def test_normal_exit_long_near_short_far(self, *args):
        """LONG near at 20000 / SHORT far at 20100, both exit up → near profit, far loss."""
        # Clean up any previous state
        for p in ["/tmp/_test_mts_fills_1.jsonl", "/tmp/_test_mts_state_1.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            # Create initial state file
            with open("/tmp/_test_mts_state_1.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-001"}, f)

            result = settle_mts_trade(
                ticker="TMF",
                trade_id="test-001",
                exit_type="NORMAL_EXIT",
                near_entry=20000.0,
                far_entry=20100.0,
                near_side="LONG",
                far_side="SHORT",
                near_exit_price=20100.0,   # +100 pts → near profit
                far_exit_price=20200.0,    # -100 pts from SHORT entry → far loss
            )

            # TMF point value = 10
            # NEAR: (20100 - 20000) * 10 = 1000 TWD
            # FAR: (20100 - 20200) * 10 = -1000 TWD (SHORT: entry - exit)
            # Gross = 0, fees ~ 80 + turnover * 4e-5
            assert result["near_realized_pnl"] == 1000.0
            assert result["far_realized_pnl"] == -1000.0
            assert result["gross_realized_pnl"] == 0.0
            assert result["net_realized_pnl"] < 0  # negative after fees
            assert result["settlement_status"] == SETTLEMENT_STATUS_SETTLED

            # Verify fills were written with realized_pnl
            fills = _read_fills("/tmp/_test_mts_fills_1.jsonl")
            exit_fills = [f for f in fills if f["fill_type"] == "EXIT" and f["trade_id"] == "test-001"]
            assert len(exit_fills) == 2
            near_fill = next(f for f in exit_fills if f["leg"] == "NEAR")
            far_fill = next(f for f in exit_fills if f["leg"] == "FAR")
            assert near_fill["realized_pnl"] is not None
            assert far_fill["realized_pnl"] is not None
        finally:
            for p in ["/tmp/_test_mts_fills_1.jsonl", "/tmp/_test_mts_state_1.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 2: Emergency close_all → SETTLED with correct PnL
# ═══════════════════════════════════════════════════════════════

class TestEmergencyCloseAll:
    """Emergency close_all: both legs filled → SETTLED."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_2.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_2.json")
    def test_emergency_close_both_legs(self, *args):
        for p in ["/tmp/_test_mts_fills_2.jsonl", "/tmp/_test_mts_state_2.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_2.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-002"}, f)

            result = settle_mts_trade(
                ticker="TMF",
                trade_id="test-002",
                exit_type="EMERGENCY_CLOSE_ALL",
                near_entry=20000.0,
                far_entry=20100.0,
                near_side="LONG",
                far_side="SHORT",
                near_exit_price=19900.0,   # -100 pts → near loss
                far_exit_price=20000.0,    # +100 pts (SHORT: 20100 - 20000) → far profit
            )

            # NEAR: (19900 - 20000) * 10 = -1000 TWD
            # FAR: (20100 - 20000) * 10 = 1000 TWD
            # Gross = 0
            assert result["near_realized_pnl"] == -1000.0
            assert result["far_realized_pnl"] == 1000.0
            assert result["gross_realized_pnl"] == 0.0
            assert result["settlement_status"] == SETTLEMENT_STATUS_SETTLED
            assert result["settled_from"] == "BROKER_FILLS"

            fills = _read_fills("/tmp/_test_mts_fills_2.jsonl")
            exit_fills = [f for f in fills if f["fill_type"] == "EXIT" and f["trade_id"] == "test-002"]
            assert len(exit_fills) == 2
            for f in exit_fills:
                assert f["realized_pnl"] is not None
                assert f["settlement_status"] == SETTLEMENT_STATUS_SETTLED
                assert f["settled_from"] == "BROKER_FILLS"
        finally:
            for p in ["/tmp/_test_mts_fills_2.jsonl", "/tmp/_test_mts_state_2.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 3: Partial fill → PARTIALLY_SETTLED
# ═══════════════════════════════════════════════════════════════

class TestPartialSettlement:
    """Only one leg has valid exit price."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_3.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_3.json")
    def test_partial_fill_one_leg_only(self, *args):
        for p in ["/tmp/_test_mts_fills_3.jsonl", "/tmp/_test_mts_state_3.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_3.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-003"}, f)

            # Only NEAR leg has a valid exit; FAR exit price = 0 (missing)
            result = settle_mts_trade(
                ticker="TMF",
                trade_id="test-003",
                exit_type="EMERGENCY_CLOSE_ALL",
                near_entry=20000.0,
                far_entry=20100.0,
                near_side="LONG",
                far_side="SHORT",
                near_exit_price=20100.0,   # valid
                far_exit_price=0.0,         # missing/no fill
            )

            assert result["settlement_status"] == SETTLEMENT_STATUS_PARTIAL
            assert result["near_realized_pnl"] == 1000.0
            assert result["far_realized_pnl"] == 0.0

            fills = _read_fills("/tmp/_test_mts_fills_3.jsonl")
            exit_fills = [f for f in fills if f["fill_type"] == "EXIT" and f["trade_id"] == "test-003"]
            # Only NEAR should have a fill record
            near_exits = [f for f in exit_fills if f["leg"] == "NEAR"]
            far_exits = [f for f in exit_fills if f["leg"] == "FAR"]
            assert len(near_exits) == 1
            assert len(far_exits) == 0
        finally:
            for p in ["/tmp/_test_mts_fills_3.jsonl", "/tmp/_test_mts_state_3.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 4: Duplicate fill callback → idempotent
# ═══════════════════════════════════════════════════════════════

class TestIdempotentSettlement:
    """Calling settle_mts_trade twice should not double-count PnL."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_4.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_4.json")
    def test_idempotent_duplicate_calls(self, *args):
        for p in ["/tmp/_test_mts_fills_4.jsonl", "/tmp/_test_mts_state_4.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_4.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-004"}, f)

            # First call
            r1 = settle_mts_trade(
                ticker="TMF",
                trade_id="test-004",
                exit_type="EMERGENCY_CLOSE_ALL",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=20100.0, far_exit_price=20200.0,
            )
            assert r1["settlement_status"] == SETTLEMENT_STATUS_SETTLED

            fills_after_first = _read_fills("/tmp/_test_mts_fills_4.jsonl")
            exit_fills_1 = [f for f in fills_after_first if f["fill_type"] == "EXIT" and f["trade_id"] == "test-004"]
            assert len(exit_fills_1) == 2

            # Second call (same params)
            r2 = settle_mts_trade(
                ticker="TMF",
                trade_id="test-004",
                exit_type="EMERGENCY_CLOSE_ALL",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=20100.0, far_exit_price=20200.0,
            )

            fills_after_second = _read_fills("/tmp/_test_mts_fills_4.jsonl")
            exit_fills_2 = [f for f in fills_after_second if f["fill_type"] == "EXIT" and f["trade_id"] == "test-004"]
            # Should still be exactly 2 — no duplicate fills
            assert len(exit_fills_2) == 2, "Idempotency broken: duplicate fills created"
            assert r2["near_realized_pnl"] == r1["near_realized_pnl"]
            assert r2["far_realized_pnl"] == r1["far_realized_pnl"]
        finally:
            for p in ["/tmp/_test_mts_fills_4.jsonl", "/tmp/_test_mts_state_4.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 5: Missing entry price → UNRESOLVED
# ═══════════════════════════════════════════════════════════════

class TestUnresolvedSettlement:
    """Missing entry price → UNRESOLVED status."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_5.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_5.json")
    def test_missing_entry_price(self, *args):
        for p in ["/tmp/_test_mts_fills_5.jsonl", "/tmp/_test_mts_state_5.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_5.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-005"}, f)

            # near_entry=0 → UNRESOLVED
            result = settle_mts_trade(
                ticker="TMF",
                trade_id="test-005",
                exit_type="EMERGENCY_CLOSE_ALL",
                near_entry=0.0,  # missing
                far_entry=0.0,   # missing
                near_side="LONG",
                far_side="SHORT",
                near_exit_price=20100.0,
                far_exit_price=20200.0,
            )

            assert result["settlement_status"] == SETTLEMENT_STATUS_UNRESOLVED
            assert result["near_realized_pnl"] == 0.0
            assert result["far_realized_pnl"] == 0.0

            fills = _read_fills("/tmp/_test_mts_fills_5.jsonl")
            exit_fills = [f for f in fills if f["fill_type"] == "EXIT" and f["trade_id"] == "test-005"]
            # Neither leg should have a fill since both entries are invalid
            assert len(exit_fills) == 0
        finally:
            for p in ["/tmp/_test_mts_fills_5.jsonl", "/tmp/_test_mts_state_5.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 6: Fees → net_realized_pnl < gross_realized_pnl
# ═══════════════════════════════════════════════════════════════

class TestFees:
    """Fees are subtracted from gross to produce net."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_6.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_6.json")
    def test_fees_subtracted(self, *args):
        for p in ["/tmp/_test_mts_fills_6.jsonl", "/tmp/_test_mts_state_6.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_6.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-006"}, f)

            result = settle_mts_trade(
                ticker="TMF",
                trade_id="test-006",
                exit_type="NORMAL_EXIT",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=20100.0, far_exit_price=20200.0,
                fees=100.0,  # explicit fee override
            )

            # Gross = 0 (near +1000, far -1000)
            # Net = -100
            assert result["gross_realized_pnl"] == 0.0
            assert result["net_realized_pnl"] == -100.0
        finally:
            for p in ["/tmp/_test_mts_fills_6.jsonl", "/tmp/_test_mts_state_6.json"]:
                if os.path.exists(p):
                    os.unlink(p)

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_6b.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_6b.json")
    def test_default_fees_auto_computed(self, *args):
        """Default fee computation matches _reset pattern."""
        for p in ["/tmp/_test_mts_fills_6b.jsonl", "/tmp/_test_mts_state_6b.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_6b.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-006b"}, f)

            result = settle_mts_trade(
                ticker="TMF",
                trade_id="test-006b",
                exit_type="NORMAL_EXIT",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=20100.0, far_exit_price=20200.0,
                # fees=0 → auto-compute
            )

            # Near: (20100+20000)*10*2e-5 = 8.02, + 40 = 48.02
            # Far: (20200+20100)*10*2e-5 = 8.06, + 40 = 48.06
            # Total fees ≈ 96.08
            assert result["net_realized_pnl"] < result["gross_realized_pnl"]
            assert result["net_realized_pnl"] < 0  # gross=0, so net is negative
        finally:
            for p in ["/tmp/_test_mts_fills_6b.jsonl", "/tmp/_test_mts_state_6b.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 7: SHORT side PnL sign (price down = profit)
# ═══════════════════════════════════════════════════════════════

class TestShortSidePnLSign:
    """SHORT leg: falling price → positive PnL."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_7.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_7.json")
    def test_short_side_price_down_profit(self, *args):
        """SHORT FAR: entry=20200, exit=20000 → profit 200 pts * 10 = 2000 TWD."""
        for p in ["/tmp/_test_mts_fills_7.jsonl", "/tmp/_test_mts_state_7.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_7.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-007"}, f)

            result = settle_mts_trade(
                ticker="TMF",
                trade_id="test-007",
                exit_type="NORMAL_EXIT",
                near_entry=20000.0, far_entry=20200.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=20100.0,   # near: +100 pts → +1000
                far_exit_price=20000.0,    # SHORT: 20200-20000 = +200 pts → +2000
            )

            assert result["near_realized_pnl"] == 1000.0
            assert result["far_realized_pnl"] == 2000.0
            assert result["gross_realized_pnl"] == 3000.0
        finally:
            for p in ["/tmp/_test_mts_fills_7.jsonl", "/tmp/_test_mts_state_7.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 8: LONG side PnL sign (price up = profit)
# ═══════════════════════════════════════════════════════════════

class TestLongSidePnLSign:
    """LONG leg: rising price → positive PnL."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_8.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_8.json")
    def test_long_side_price_up_profit(self, *args):
        """LONG NEAR: entry=20000, exit=20500 → profit 500 pts * 10 = 5000 TWD."""
        for p in ["/tmp/_test_mts_fills_8.jsonl", "/tmp/_test_mts_state_8.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_8.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-008"}, f)

            result = settle_mts_trade(
                ticker="TMF",
                trade_id="test-008",
                exit_type="NORMAL_EXIT",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=20500.0,   # LONG: +500 pts → +5000
                far_exit_price=20000.0,    # SHORT: +100 pts → +1000
            )

            assert result["near_realized_pnl"] == 5000.0
            assert result["far_realized_pnl"] == 1000.0
            assert result["gross_realized_pnl"] == 6000.0
        finally:
            for p in ["/tmp/_test_mts_fills_8.jsonl", "/tmp/_test_mts_state_8.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 9: Normal EXIT vs Emergency EXIT same PnL
# ═══════════════════════════════════════════════════════════════

class TestExitTypeComparability:
    """Same fills with different exit_type produce same PnL."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_9.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_9.json")
    def test_normal_and_emergency_same_pnl(self, *args):
        for p in ["/tmp/_test_mts_fills_9.jsonl", "/tmp/_test_mts_state_9.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_9.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-009"}, f)

            r_normal = settle_mts_trade(
                ticker="TMF",
                trade_id="test-009a",
                exit_type="NORMAL_EXIT",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=19900.0, far_exit_price=20000.0,
            )

            r_emergency = settle_mts_trade(
                ticker="TMF",
                trade_id="test-009b",
                exit_type="EMERGENCY_CLOSE_ALL",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=19900.0, far_exit_price=20000.0,
            )

            # PnL should be identical regardless of exit_type label
            assert r_normal["near_realized_pnl"] == r_emergency["near_realized_pnl"]
            assert r_normal["far_realized_pnl"] == r_emergency["far_realized_pnl"]
            assert r_normal["gross_realized_pnl"] == r_emergency["gross_realized_pnl"]
        finally:
            for p in ["/tmp/_test_mts_fills_9.jsonl", "/tmp/_test_mts_state_9.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test 10: Restart replay doesn't re-accumulate PnL
# ═══════════════════════════════════════════════════════════════

class TestRestartReplay:
    """Calling settle_mts_trade after restart (with fills already on disk) is idempotent."""

    @patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", new_callable=lambda: "/tmp/_test_mts_fills_10.jsonl")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_EVENT_LOG", new_callable=lambda: "/dev/null")
    @patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", new_callable=lambda: "/tmp/_test_mts_state_10.json")
    def test_restart_replay_no_double_pnl(self, *args):
        """Simulate: first call settles, then on restart settle is called again."""
        for p in ["/tmp/_test_mts_fills_10.jsonl", "/tmp/_test_mts_state_10.json"]:
            if os.path.exists(p):
                os.unlink(p)

        try:
            with open("/tmp/_test_mts_state_10.json", "w") as f:
                json.dump({"has_position": True, "state": "OPEN", "trade_id": "test-010"}, f)

            # First settlement (like initial close_all)
            r1 = settle_mts_trade(
                ticker="TMF", trade_id="test-010",
                exit_type="EMERGENCY_CLOSE_ALL",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=20100.0, far_exit_price=20200.0,
            )

            fills_after_first = _read_fills("/tmp/_test_mts_fills_10.jsonl")
            exit_1 = [f for f in fills_after_first if f["fill_type"] == "EXIT"]
            assert len(exit_1) == 2

            # Simulate PM2 restart: new process, fills log persists on disk
            # Call settle again (monitor._restore_position_state → triggers settle)
            r2 = settle_mts_trade(
                ticker="TMF", trade_id="test-010",
                exit_type="EMERGENCY_CLOSE_ALL",
                near_entry=20000.0, far_entry=20100.0,
                near_side="LONG", far_side="SHORT",
                near_exit_price=20100.0, far_exit_price=20200.0,
            )

            fills_after_second = _read_fills("/tmp/_test_mts_fills_10.jsonl")
            exit_2 = [f for f in fills_after_second if f["fill_type"] == "EXIT"]
            # Must still be exactly 2 — no duplicate fills from restart replay
            assert len(exit_2) == 2, "Restart replay created duplicate fills"

            # PnL values must be identical between first and second call
            # (idempotent — no double-counting)
            assert r1["near_realized_pnl"] == r2["near_realized_pnl"]
            assert r1["far_realized_pnl"] == r2["far_realized_pnl"]
            assert r1["gross_realized_pnl"] == r2["gross_realized_pnl"]
            assert r1["net_realized_pnl"] == r2["net_realized_pnl"]
            assert r1["settlement_status"] == r2["settlement_status"]
        finally:
            for p in ["/tmp/_test_mts_fills_10.jsonl", "/tmp/_test_mts_state_10.json"]:
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════
# Test _has_settled_exit helper
# ═══════════════════════════════════════════════════════════════

class TestHasSettledExit:
    """Unit tests for the _has_settled_exit idempotency check."""

    def test_no_fill_log_returns_false(self, tmp_fill_log):
        """No file → False."""
        assert _has_settled_exit("test-tid", "NEAR", log_path=tmp_fill_log) is False

    def test_empty_fill_log_returns_false(self, tmp_fill_log):
        """Empty file → False."""
        with open(tmp_fill_log, "w") as f:
            f.write("")
        assert _has_settled_exit("test-tid", "NEAR", log_path=tmp_fill_log) is False

    def test_matching_settled_exit_returns_true(self, tmp_fill_log):
        """Exists with realized_pnl → True."""
        with open(tmp_fill_log, "w") as f:
            f.write(json.dumps({
                "trade_id": "test-tid", "leg": "NEAR",
                "fill_type": "EXIT", "realized_pnl": 100.0,
            }) + "\n")
        assert _has_settled_exit("test-tid", "NEAR", log_path=tmp_fill_log) is True

    def test_exit_without_realized_pnl_returns_false(self, tmp_fill_log):
        """EXIT fill with realized_pnl=None → False (not settled)."""
        with open(tmp_fill_log, "w") as f:
            f.write(json.dumps({
                "trade_id": "test-tid", "leg": "NEAR",
                "fill_type": "EXIT", "realized_pnl": None,
            }) + "\n")
        assert _has_settled_exit("test-tid", "NEAR", log_path=tmp_fill_log) is False

    def test_wrong_leg_returns_false(self, tmp_fill_log):
        """FAR exit doesn't count for NEAR."""
        with open(tmp_fill_log, "w") as f:
            f.write(json.dumps({
                "trade_id": "test-tid", "leg": "FAR",
                "fill_type": "EXIT", "realized_pnl": 100.0,
            }) + "\n")
        assert _has_settled_exit("test-tid", "NEAR", log_path=tmp_fill_log) is False

    def test_wrong_fill_type_returns_false(self, tmp_fill_log):
        """ENTRY fill doesn't count as EXIT."""
        with open(tmp_fill_log, "w") as f:
            f.write(json.dumps({
                "trade_id": "test-tid", "leg": "NEAR",
                "fill_type": "ENTRY", "realized_pnl": 100.0,
            }) + "\n")
        assert _has_settled_exit("test-tid", "NEAR", log_path=tmp_fill_log) is False


# ═══════════════════════════════════════════════════════════════
# Test settlement constants
# ═══════════════════════════════════════════════════════════════

class TestSettlementConstants:
    """Verify settlement status string constants."""

    def test_constants_values(self):
        assert SETTLEMENT_STATUS_PENDING == "PENDING_SETTLEMENT"
        assert SETTLEMENT_STATUS_PARTIAL == "PARTIALLY_SETTLED"
        assert SETTLEMENT_STATUS_SETTLED == "SETTLED"
        assert SETTLEMENT_STATUS_UNRESOLVED == "UNRESOLVED"
