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


def normalize_trade_status(trade: Any) -> str:
    """Canonical leaf status name from a Shioaji-like trade.

    Handles nested ``trade.status.status``, dict-wrapped status, enums
    (name/value) and qualified enum names (``X.Y.Status`` -> ``Status``).
    Missing status -> "" (never terminal).
    """
    status = getattr(trade, "status", None)
    nested = getattr(status, "status", None)
    raw = nested if nested is not None else status
    if isinstance(raw, dict):
        raw = raw.get("status", "")
    name = (getattr(raw, "name", None)
            or getattr(raw, "value", None)
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
    order = getattr(trade, "order", None)
    ordno = (getattr(trade, "ordno", None)
             or getattr(order, "ordno", None))
    id_ = (getattr(trade, "id", None)
           or getattr(order, "id", None))
    broker_id = (getattr(trade, "broker_order_id", None)
                 or getattr(order, "id", None))
    seqno = (getattr(trade, "seqno", None)
             or getattr(order, "seqno", None))
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
    order = getattr(trade, "order", None)
    return {
        "id": getattr(trade, "id", None)
              or getattr(trade, "broker_order_id", None)
              or getattr(trade, "exchange_order_id", None)
              or getattr(order, "id", None),
        "broker_order_id": getattr(trade, "broker_order_id", None)
                           or getattr(trade, "id", None)
                           or getattr(order, "id", None),
        "ordno": getattr(trade, "ordno", None)
                 or getattr(order, "ordno", None),
        "seqno": getattr(trade, "seqno", None)
                 or getattr(order, "seqno", None),
        "code": getattr(trade, "code", None)
                or getattr(getattr(order, "contract", None), "code", None),
        "status": normalize_trade_status(trade),
        "quantity": getattr(trade, "quantity", None),
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
    open_orders = [
        normalize_trade_row(t)
        for t in dedupe_trades(trades)
        if not is_terminal_status(normalize_trade_status(t))
    ]
    return {
        "source": "live_broker",
        "session_id": session_id,
        "captured_at": captured_at,
        "positions": [normalize_position_row(p) for p in positions or []],
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
