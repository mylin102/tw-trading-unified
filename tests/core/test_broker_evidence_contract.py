"""RED/GREEN tests for core/broker_evidence.py — Evidence Contract (GSD Phase 1).

Contract points under test (codex handoff, fake-API only):
1. one session-bound broker snapshot — every snapshot carries its session_id;
   evidence is never merged across sessions.
2. dedupe list_trades identities — rows with the same raw broker identity
   (ordno / broker_order_id / seqno / id) collapse to one.
3. normalize Shioaji nested trade.status.status plus enum name/value.
4. preserve raw broker identity — id / broker_order_id / ordno / seqno survive
   normalization verbatim (top-level or nested order).
5. PendingSubmit is never terminal — pending rows stay open, never dropped.
6. query failure is typed/unavailable and fail-closed — an api exception
   yields a typed unavailable payload, never empty-as-flat.
"""
from types import SimpleNamespace

import pytest

from core.broker_evidence import (
    build_session_snapshot,
    capture_session_snapshot,
    dedupe_trades,
    is_terminal_status,
    normalize_trade_row,
    normalize_trade_status,
    trade_identity,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _trade(**kw):
    """One Shioaji-like trade row: top-level fields + nested order/status."""
    base = {
        "id": "2353c7b0",
        "ordno": "2353c7b0",
        "seqno": "756569",
        "code": "TMFI6",
        "quantity": 1,
        "status": SimpleNamespace(status="Filled"),
        "order": SimpleNamespace(
            id="2353c7b0", ordno="2353c7b0", seqno="756569",
            contract=SimpleNamespace(code="TMFI6")),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _pending_trade():
    return _trade(status=SimpleNamespace(status="PendingSubmit"))


def _fake_api(positions=None, trades=None, *, raise_positions=False,
              raise_trades=False, no_methods=False):
    if no_methods:
        return SimpleNamespace(futopt_account=SimpleNamespace(account_no="F-1"))
    return _FakeApi(
        positions=positions or [],
        trades=trades or [],
        raise_positions=raise_positions,
        raise_trades=raise_trades,
    )


class _FakeApi:
    """Keyword-scoped fake api (mirrors test_capture_orders_identity)."""

    def __init__(self, *, positions, trades, raise_positions=False,
                 raise_trades=False):
        self._positions = positions
        self._trades = trades
        self._raise_positions = raise_positions
        self._raise_trades = raise_trades
        self.calls = []

    @property
    def futopt_account(self):
        return SimpleNamespace(account_no="F-1")

    @property
    def stock_account(self):
        return None

    def list_positions(self, account=None):
        self.calls.append(("list_positions", account))
        if self._raise_positions:
            raise RuntimeError("positions query failed")
        return self._positions

    def list_trades(self, account=None):
        self.calls.append(("list_trades", account))
        if self._raise_trades:
            raise RuntimeError("trades query failed")
        return self._trades


# ---------------------------------------------------------------------------
# 1. session-bound broker snapshot
# ---------------------------------------------------------------------------

def test_snapshot_carries_session_id():
    snap = build_session_snapshot(
        session_id="sess-A", positions=[], trades=[], captured_at=1_720_000_000_000)
    assert snap["session_id"] == "sess-A"
    assert snap["source"] == "live_broker"
    assert snap["captured_at"] == 1_720_000_000_000


def test_snapshots_never_merge_across_sessions():
    a = build_session_snapshot(
        session_id="sess-A",
        positions=[SimpleNamespace(code="TMFI6", quantity=1,
                                   direction=SimpleNamespace(name="Sell"),
                                   price=46000.0)],
        trades=[_trade()], captured_at=1_720_000_000_000)
    b = build_session_snapshot(
        session_id="sess-B", positions=[], trades=[], captured_at=1_720_000_000_001)
    assert a["session_id"] == "sess-A"
    assert b["session_id"] == "sess-B"
    assert b["positions"] == []
    # evidence from session A never leaks into B
    assert all(p["code"] != "TMFI6" for p in b["positions"])


def test_capture_session_snapshot_binds_session():
    api = _fake_api(positions=[], trades=[_pending_trade()])
    snap = capture_session_snapshot(api, session_id="sess-C")
    assert snap["session_id"] == "sess-C"
    assert snap["source"] == "live_broker"


# ---------------------------------------------------------------------------
# 2. dedupe list_trades identities
# ---------------------------------------------------------------------------

def test_dedupe_collapses_same_broker_identity():
    rows = [_trade(), _trade()]
    assert len(dedupe_trades(rows)) == 1


def test_dedupe_keeps_distinct_identities():
    rows = [_trade(ordno="A", id="A"),
            _trade(ordno="B", id="B", seqno="2")]
    assert len(dedupe_trades(rows)) == 2


def test_dedupe_uses_nested_order_identity():
    rows = [
        _trade(id=None, ordno=None, seqno=None),
        _trade(id=None, ordno=None, seqno=None),
    ]
    # identity resolved from nested order.id when top-level is absent
    assert len(dedupe_trades(rows)) == 1


# ---------------------------------------------------------------------------
# 3. normalize nested trade.status.status + enum name/value
# ---------------------------------------------------------------------------

def test_normalize_nested_status():
    assert normalize_trade_status(_trade()) == "Filled"


def test_normalize_enum_name_and_value():
    enum_like = SimpleNamespace(name="Filled", value="Filled")
    assert normalize_trade_status(_trade(status=enum_like)) == "Filled"


def test_normalize_qualified_enum_name():
    # Shioaji enums can surface as qualified "FuturesOrderStatus.Filled"
    assert normalize_trade_status(
        _trade(status=SimpleNamespace(status="FuturesOrderStatus.Filled"))) \
        == "Filled"


def test_normalize_dict_status():
    assert normalize_trade_status(
        _trade(status=SimpleNamespace(status={"status": "Filled"}))) == "Filled"


def test_normalize_unknown_status_is_empty_not_terminal():
    assert normalize_trade_status(_trade(status=None)) == ""
    assert not is_terminal_status("")


# ---------------------------------------------------------------------------
# 4. preserve raw broker identity
# ---------------------------------------------------------------------------

def test_normalize_row_preserves_raw_identity():
    row = normalize_trade_row(_trade())
    assert row["id"] == "2353c7b0"
    assert row["broker_order_id"] == "2353c7b0"
    assert row["ordno"] == "2353c7b0"
    assert row["seqno"] == "756569"
    assert row["code"] == "TMFI6"


def test_normalize_row_resolves_nested_identity():
    row = normalize_trade_row(_trade(id=None, broker_order_id=None))
    assert row["broker_order_id"] == "2353c7b0"  # from nested order.id


# ---------------------------------------------------------------------------
# 5. PendingSubmit is never terminal
# ---------------------------------------------------------------------------

def test_pending_submit_not_terminal():
    assert not is_terminal_status("PendingSubmit")
    assert not is_terminal_status("pending_submit")
    assert not is_terminal_status(
        normalize_trade_status(_pending_trade()))


def test_pending_rows_survive_open_orders():
    snap = build_session_snapshot(
        session_id="s", positions=[],
        trades=[_pending_trade(), _trade()], captured_at=1)
    statuses = {o["status"] for o in snap["open_orders"]}
    assert "PendingSubmit" in statuses
    assert "Filled" not in statuses


def test_terminal_set_contains_no_pending():
    from core.broker_evidence import TERMINAL_TRADE_STATUSES
    assert "PendingSubmit" not in TERMINAL_TRADE_STATUSES
    assert "pending_submit" not in TERMINAL_TRADE_STATUSES


# ---------------------------------------------------------------------------
# 6. query failure is typed/unavailable and fail-closed
# ---------------------------------------------------------------------------

def test_positions_query_failure_is_typed_unavailable():
    snap = capture_session_snapshot(
        _fake_api(raise_positions=True), session_id="s")
    assert snap["source"] == "unavailable"
    assert snap["capture_error"] is True
    assert "list_positions" in snap.get("error", "")
    # fail-closed: NOT an empty positions list that reads as flat
    assert "positions" not in snap or snap.get("positions") is None


def test_trades_query_failure_is_typed_unavailable():
    snap = capture_session_snapshot(
        _fake_api(raise_trades=True), session_id="s")
    assert snap["source"] == "unavailable"
    assert snap["capture_error"] is True
    assert "list_trades" in snap.get("error", "")


def test_missing_api_method_is_typed_unavailable():
    snap = capture_session_snapshot(_fake_api(no_methods=True), session_id="s")
    assert snap["source"] == "unavailable"
    assert snap["capture_error"] is True


def test_session_id_required_for_capture():
    with pytest.raises(TypeError):
        capture_session_snapshot(_fake_api())


def test_build_requires_session_id():
    with pytest.raises(TypeError):
        build_session_snapshot(positions=[], trades=[], captured_at=1)


# ---------------------------------------------------------------------------
# 7. identity-less rows are never silently deduped (codex P1, fail-closed)
# ---------------------------------------------------------------------------

def _anonymous_trade():
    """A row with NO broker identity anywhere (top-level or nested order)."""
    return SimpleNamespace(
        id=None, ordno=None, seqno=None, broker_order_id=None,
        code="TMFI6", quantity=1, status=SimpleNamespace(status="PendingSubmit"),
        order=None)


def test_trade_identity_none_for_identity_less_row():
    # must be None (typed missing), NOT ("", "", "", "") — a shared empty
    # tuple would make every anonymous row look like the same order
    assert trade_identity(_anonymous_trade()) is None


def test_identity_less_rows_never_collapsed():
    # two distinct orders that both lack identity must BOTH survive
    # (fail-closed: no proof they are the same order)
    assert len(dedupe_trades([_anonymous_trade(), _anonymous_trade()])) == 2


def test_mixed_identity_and_anonymous_rows_all_kept():
    rows = [_trade(), _anonymous_trade(), _anonymous_trade()]
    assert len(dedupe_trades(rows)) == 3


def test_normalized_anonymous_row_flagged_identity_missing():
    row = normalize_trade_row(_anonymous_trade())
    assert row.get("identity_missing") is True


def test_identified_row_not_flagged_identity_missing():
    assert normalize_trade_row(_trade()).get("identity_missing") is False


# ---------------------------------------------------------------------------
# 8. empty/None session_id is typed invalid, never live_broker (codex gap)
# ---------------------------------------------------------------------------

def test_build_snapshot_empty_session_is_typed_invalid():
    snap = build_session_snapshot(
        session_id="", positions=[], trades=[], captured_at=1)
    assert snap["source"] != "live_broker"
    assert snap["capture_error"] is True
    assert "session_id" in snap.get("error", "")


def test_build_snapshot_none_session_is_typed_invalid():
    snap = build_session_snapshot(
        session_id=None, positions=[], trades=[], captured_at=1)
    assert snap["source"] != "live_broker"
    assert snap["capture_error"] is True


def test_capture_empty_session_is_typed_invalid_no_api_calls():
    api = _fake_api(positions=[], trades=[_pending_trade()])
    snap = capture_session_snapshot(api, session_id="")
    assert snap["source"] != "live_broker"
    assert snap["capture_error"] is True
    assert "session_id" in snap.get("error", "")
    # fail-closed: never queries the broker for an invalid session
    assert api.calls == []
