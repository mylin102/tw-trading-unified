"""Evidence Contract — canonical broker-evidence normalization (GSD Phase 1).

Pure, UNCONNECTED contract layer.  No imports from monitor / preflight /
order_manager; no broker calls at import time; no wiring into any runtime
path.  Phase 2 (codex) connects these primitives.

Contract points (codex GSD Phase 1 handoff):
1. one session-bound broker snapshot — every snapshot carries its
   ``session_id``; evidence is never merged across sessions.
2. dedupe list_trades identities — rows sharing raw broker identity
   (ordno / broker_order_id / seqno / id) collapse to one; identity is
   resolved from nested ``order`` fields when the top level is absent.
3. normalize Shioaji nested ``trade.status.status`` plus enum name/value
   (qualified enum names such as ``FuturesOrderStatus.Filled`` collapse
   to the leaf name).
4. preserve raw broker identity — id / broker_order_id / ordno / seqno
   survive normalization verbatim.
5. PendingSubmit is never terminal — pending rows stay open and are
   never dropped from open_orders.
6. query failure is typed/unavailable and fail-closed — an api exception
   yields ``{"source": "unavailable", "capture_error": True, ...}``,
   never an empty positions/open_orders that reads as flat.
"""
from __future__ import annotations

import time
from typing import Any

# Terminal statuses used by the existing snapshot paths.  PendingSubmit is
# deliberately ABSENT: a pending row is open evidence, never terminal.
TERMINAL_TRADE_STATUSES = frozenset({"Filled", "Cancelled", "Expired", "Done"})


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a Shioaji field from either an object or a dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def normalize_trade_status(trade: Any) -> str:
    """Canonical leaf status name from a Shioaji-like trade.

    Handles nested ``trade.status.status``, dict-wrapped status, enums
    (name/value) and qualified enum names (``X.Y.Status`` -> ``Status``).
    Missing status -> "" (never terminal).
    """
    status = _field(trade, "status")
    nested = _field(status, "status")
    raw = nested if nested is not None else status
    if isinstance(raw, dict):
        raw = raw.get("status", "")
    name = (_field(raw, "name")
            or _field(raw, "value")
            or str(raw or ""))
    return str(name).split(".")[-1]


def is_terminal_status(status: str) -> bool:
    """True only for the terminal set.  PendingSubmit never qualifies."""
    return status in TERMINAL_TRADE_STATUSES


def trade_identity(trade: Any) -> tuple[str, str, str, str] | None:
    """Raw broker identity: (ordno, id, broker_order_id, seqno).

    Nested ``order`` fields are the fallback for Shioaji 1.7 trades that
    omit top-level identity.

    Returns None when NO identity field is present anywhere (top-level and
    nested) — an identity-less row is non-reconcilable and must NEVER be
    collapsed against another identity-less row (fail-closed; distinct
    orders without identity must all survive).
    """
    order = _field(trade, "order")
    status = _field(trade, "status")
    ordno = (_field(trade, "ordno") or _field(order, "ordno"))
    id_ = (_field(trade, "id") or _field(order, "id")
           or _field(status, "id"))
    broker_id = (_field(trade, "broker_order_id")
                 or _field(order, "id") or _field(status, "id"))
    seqno = (_field(trade, "seqno") or _field(order, "seqno"))
    if ordno is None and id_ is None and broker_id is None and seqno is None:
        return None
    return (str(ordno or ""), str(id_ or ""),
            str(broker_id or ""), str(seqno or ""))


def dedupe_trades(trades: list[Any]) -> list[Any]:
    """Collapse rows sharing the same raw broker identity (first wins).

    Identity-less rows (``trade_identity`` is None) are NEVER collapsed —
    each is retained separately so a distinct order without broker
    identity can never be dropped (fail-closed).
    """
    seen: set[tuple[str, str, str, str]] = set()
    out: list[Any] = []
    for trade in trades or []:
        ident = trade_identity(trade)
        if ident is None:
            out.append(trade)  # non-reconcilable: keep every row
            continue
        if ident in seen:
            continue
        seen.add(ident)
        out.append(trade)
    return out


def normalize_trade_row(trade: Any) -> dict[str, Any]:
    """Canonical trade evidence row with raw identity preserved.

    Rows with no broker identity anywhere are flagged
    ``identity_missing: True`` (typed disposition — fail-closed, never
    silently treated as a known order).
    """
    order = _field(trade, "order")
    status = _field(trade, "status")
    contract = _field(trade, "contract") or _field(order, "contract")
    broker_id = (_field(trade, "broker_order_id")
                 or _field(order, "id") or _field(status, "id"))
    ordno = _field(trade, "ordno") or _field(order, "ordno")
    seqno = _field(trade, "seqno") or _field(order, "seqno")
    requested_qty = (_field(order, "quantity")
                     if _field(order, "quantity") is not None
                     else _field(status, "order_quantity"))
    filled_qty = _field(status, "deal_quantity")
    cancelled_qty = _field(status, "cancel_quantity")
    deals = _field(status, "deals")
    return {
        "id": _field(trade, "id") or broker_id,
        "broker_order_id": broker_id,
        "status_id": _field(status, "id"),
        "ordno": ordno,
        "seqno": seqno,
        "code": _field(contract, "code") or _field(trade, "code"),
        "status": normalize_trade_status(trade),
        "broker_status": normalize_trade_status(trade),
        "status_code": _field(status, "status_code"),
        "quantity": requested_qty,
        "requested_qty": requested_qty,
        "filled_quantity": filled_qty,
        "filled_qty": filled_qty,
        "cancelled_qty": cancelled_qty,
        "deals": list(deals) if isinstance(deals, (list, tuple)) else deals,
        "identity_missing": trade_identity(trade) is None,
    }


def normalize_position_row(pos: Any, account_tag: str = "futures") -> dict[str, Any]:
    """Canonical position row; NEVER raw account numbers."""
    return {
        "account": account_tag,
        "code": str(getattr(pos, "code", "")),
        "quantity": int(getattr(pos, "quantity", 0) or 0),
        "direction": str(
            getattr(getattr(pos, "direction", None), "name", None)
            or str(getattr(pos, "direction", ""))).rsplit(".", 1)[-1].lower(),
    }


def _invalid_session(session_id: str | None) -> dict[str, Any]:
    """Typed fail-closed payload for an empty/missing session identity.

    Session-bound isolation: a snapshot without a non-empty session_id is
    NOT broker evidence — never ``source=live_broker``.
    """
    return {
        "source": "invalid_session",
        "capture_error": True,
        "session_id": session_id,
        "error": "session_id is empty (session-bound isolation)",
    }


def build_session_snapshot(*, session_id: str, positions: list[Any],
                           trades: list[Any], captured_at: int) -> dict[str, Any]:
    """One session-bound broker snapshot (pure).

    Positions are normalized; trades are deduped and only NON-terminal rows
    (PendingSubmit included) become open_orders.

    Empty/None session_id -> typed invalid_session payload (fail-closed,
    never ``source=live_broker``).
    """
    if not session_id:
        return _invalid_session(session_id)
    normalized_trades = [normalize_trade_row(t) for t in dedupe_trades(trades)]
    open_orders = [
        row for row in normalized_trades
        if not is_terminal_status(row["status"])
    ]
    return {
        "source": "live_broker",
        "session_id": session_id,
        "captured_at": captured_at,
        "positions": [normalize_position_row(p) for p in positions or []],
        # Keep terminal broker evidence for lifecycle reconciliation.  The
        # open_orders projection remains non-terminal for watchdog consumers.
        "trades": normalized_trades,
        "open_orders": open_orders,
    }


def capture_session_snapshot(api: Any, *, session_id: str) -> dict[str, Any]:
    """Read-only session-bound capture, fail-closed.

    Zero place/cancel/update calls.  Any query exception -> typed
    unavailable payload (never empty-as-flat).  Empty/None session_id ->
    typed invalid_session payload WITHOUT querying the broker.
    """
    if not session_id:
        return _invalid_session(session_id)
    if not hasattr(api, "list_positions") or not hasattr(api, "list_trades"):
        return {
            "source": "unavailable",
            "capture_error": True,
            "session_id": session_id,
            "error": "api has no list_positions/list_trades",
        }
    account = getattr(api, "futopt_account", None)
    positions: list[Any] = []
    trades: list[Any] = []
    try:
        try:
            positions = list(api.list_positions(account=account))
        except TypeError:
            positions = list(api.list_positions())
    except Exception as exc:
        return {
            "source": "unavailable",
            "capture_error": True,
            "session_id": session_id,
            "error": f"list_positions: {type(exc).__name__}: {exc}",
        }
    try:
        try:
            trades = list(api.list_trades(account=account))
        except TypeError:
            trades = list(api.list_trades())
    except Exception as exc:
        return {
            "source": "unavailable",
            "capture_error": True,
            "session_id": session_id,
            "error": f"list_trades: {type(exc).__name__}: {exc}",
        }
    return build_session_snapshot(
        session_id=session_id,
        positions=positions,
        trades=trades,
        captured_at=int(time.time() * 1000),
    )



def _row_value(row: Any, *names: str) -> Any:
    """First non-empty value among names (dict or object)."""
    if isinstance(row, dict):
        for name in names:
            v = row.get(name)
            if v not in (None, ""):
                return v
        return None
    for name in names:
        v = getattr(row, name, None)
        if v not in (None, ""):
            return v
    return None


def _row_identities(row: Any) -> set[str]:
    """Broker-issued identity fields only; local order_id excluded.

    Falls back to nested ``order`` fields for Shioaji 1.7 trades that omit
    top-level identity.
    """
    order = _row_value(row, "order")
    order = order if isinstance(order, (dict,)) or hasattr(order, "__dict__") else None
    values = {
        _row_value(row, "broker_order_id", "id", "exchange_order_id")
        or (_row_value(order, "broker_order_id", "id", "exchange_order_id") if order else None),
        _row_value(row, "ordno") or (_row_value(order, "ordno") if order else None),
        _row_value(row, "seqno") or (_row_value(order, "seqno") if order else None),
        _row_value(row, "deal_id", "trade_id", "fill_id"),
    }
    return {str(v) for v in values if v not in (None, "")}


def _row_symbol(row: Any) -> str:
    return str(_row_value(row, "symbol", "code") or "")


def _row_qty(row: Any) -> int | None:
    v = _row_value(row, "quantity", "qty")
    try:
        if isinstance(v, bool):
            return None
        q = int(v)
        return q if q > 0 else None
    except (TypeError, ValueError):
        return None


def _row_direction(row: Any) -> str | None:
    raw = str(_row_value(row, "direction", "side", "action") or "").lower()
    if "sell" in raw or "short" in raw:
        return "sell"
    if "buy" in raw or "long" in raw:
        return "buy"
    return None


def open_orders_fully_covered_by_filled(open_orders: list[Any],
                                        filled_orders: list[Any]) -> bool:
    """True ONLY when every open order is explained one-to-one by a local
    FILLED order (identity + symbol + direction + quantity).

    Stale ``PendingSubmit`` rows retained by a Shioaji session after a fill
    are safe to treat as position-covered ONLY when each one matches an
    explicit local terminal fill.  Fail-closed: an identity-less open order,
    a direction/quantity/symbol mismatch, a non-terminal local order, or an
    unmatched open order (real pending exit / add-on / reverse) all return
    False so the caller keeps the unresolved/never-flat verdict.
    """
    if not open_orders:
        return True
    # Dedupe open_orders by broker identity first: repeated capture of the
    # same order is ONE canonical pending, not N distinct orders (Phase 2
    # spec 2.1).  Rows sharing an identity must agree on symbol/qty/direction,
    # else the evidence is ambiguous and nothing is covered.
    groups: dict[tuple[str, ...], list[Any]] = {}
    for oo in open_orders:
        oo_ids = _row_identities(oo)
        if not oo_ids:
            return False  # no broker identity -> cannot prove covered
        key = tuple(sorted(oo_ids))
        groups.setdefault(key, []).append(oo)
    for key, rows in groups.items():
        first = rows[0]
        sym = _row_symbol(first)
        qty = _row_qty(first)
        direction = _row_direction(first)
        if not sym or qty is None or direction is None:
            return False
        for extra in rows[1:]:
            if (_row_symbol(extra) != sym or _row_qty(extra) != qty
                    or _row_direction(extra) != direction):
                return False  # same identity, conflicting facts
    used: set[int] = set()
    for key, rows in groups.items():
        sym = _row_symbol(rows[0])
        qty = _row_qty(rows[0])
        direction = _row_direction(rows[0])
        matched = False
        for idx, fo in enumerate(filled_orders):
            if idx in used:
                continue
            fo_status = str(_row_value(fo, "status") or "")
            if fo_status.lower() not in ("filled", "fill", "complete", "completed"):
                continue  # no terminal fill proof -> cannot cover
            if not (set(key) & _row_identities(fo)):
                continue
            if _row_symbol(fo) != sym:
                continue
            if _row_qty(fo) != qty:
                continue
            if _row_direction(fo) != direction:
                continue
            used.add(idx)
            matched = True
            break
        if not matched:
            return False
    return True
