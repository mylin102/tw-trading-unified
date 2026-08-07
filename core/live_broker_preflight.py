"""One-shot, read-only broker preflight for a future paper-to-live transition.

This module deliberately does not import the strategy, OrderManager, or any
order-routing code.  It is evidence collection only; a successful result does
not change ``live_trading`` or grant live-order authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contract_resolver import ContractResolver
from core.deployment_role_gate import assert_broker_access_allowed
from core.runtime_paths import runtime_path


REQUEST_ENV = "LIVE_PREFLIGHT_NO_ORDERS"
LOCK_NAME = "live_preflight.lock"


class PreflightBlocked(RuntimeError):
    """Raised before a broker session is created when the safety contract fails."""


def diagnostics_dir() -> Path:
    return Path(runtime_path("exports", "trades", "live", "diagnostics"))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _account_hash(account: Any) -> str:
    raw = ":".join(str(getattr(account, attr, "")) for attr in ("person_id", "broker_id", "account_id"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _contract_summary(contract: Any) -> dict[str, Any]:
    return {
        "code": getattr(contract, "code", None),
        "delivery_date": str(getattr(contract, "delivery_date", "")),
        "category": getattr(contract, "category", None),
    }


def _safe_positions(api: Any, account: Any) -> list[dict[str, Any]]:
    return [
        {"code": getattr(p, "code", None), "qty": getattr(p, "quantity", None), "pnl": getattr(p, "pnl", None)}
        for p in list(api.list_positions(account))
    ]


def _safe_open_orders(api: Any, account: Any) -> list[dict[str, Any]]:
    try:
        trades = list(api.list_trades())
    except TypeError:
        trades = list(api.list_trades(account))
    terminal = {"Filled", "Cancelled", "Expired", "Done"}
    return [
        {
            "code": getattr(t, "code", None),
            "qty": getattr(t, "quantity", None),
            "status": getattr(getattr(t, "status", None), "status", None),
        }
        for t in trades
        if getattr(getattr(t, "status", None), "status", "") not in terminal
    ]


def _unsubscribe_bidask(api: Any, contract: Any) -> None:
    """Use the matching Shioaji unsubscribe surface for the installed SDK."""
    from core.broker.shioaji_compat import is_rust_version
    if is_rust_version():
        import shioaji as sj
        api.unsubscribe(contract, quote_type=sj.QuoteType.BidAsk, version=sj.QuoteVersion.v1)
    else:
        import shioaji as sj
        api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)


def collect_read_only_preflight(api: Any, product: str = "TMF") -> dict[str, Any]:
    """Collect only account, contract, query, snapshot and quote-capability evidence.

    There is intentionally no order/cancel/modify method in this adapter.
    """
    account = getattr(api, "futopt_account", None)
    if account is None:
        raise PreflightBlocked("FUTOPT_ACCOUNT_UNAVAILABLE")

    resolver = ContractResolver(api)
    near, far = resolver.get_near_far_contracts(product)
    if near is None or far is None:
        raise PreflightBlocked("NEAR_FAR_CONTRACT_RESOLUTION_FAILED")

    positions = _safe_positions(api, account)
    open_orders = _safe_open_orders(api, account)
    margin = api.margin(account)
    limits = api.trading_limits(account)
    snapshots = list(api.snapshots([near, far]))

    # Subscription proves the broker accepts the request.  It is immediately
    # removed and does not wait for market data (closed-market safe).
    from core.broker.shioaji_compat import safe_subscribe
    quote_checks: list[dict[str, Any]] = []
    for contract in (near, far):
        try:
            safe_subscribe(api, contract, quote_type="bidask")
            try:
                _unsubscribe_bidask(api, contract)
            except Exception:
                # A successful subscription is the capability proof; an
                # unsubscribe failure is still reported and fails the check.
                raise
            quote_checks.append({"code": contract.code, "passed": True})
        except Exception as exc:
            quote_checks.append({"code": contract.code, "passed": False, "error": str(exc)})

    return {
        "connected": True,
        "authenticated": True,
        "account_id_hash": _account_hash(account),
        "position_snapshot_time": datetime.now(timezone.utc).isoformat(),
        "order_snapshot_time": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
        "open_orders": open_orders,
        "margin": {
            "available_margin": getattr(margin, "available_margin", None),
            "equity_amount": getattr(margin, "equity_amount", None),
            "risk_indicator": getattr(margin, "risk_indicator", None),
        },
        "trading_limits": str(limits),
        "contracts": {"near": _contract_summary(near), "far": _contract_summary(far)},
        "snapshot_codes": [getattr(s, "code", None) for s in snapshots],
        "quote_subscription": quote_checks,
    }


def run_once(api_factory: Any, *, request_id: str | None = None, product: str = "TMF") -> dict[str, Any]:
    """Run a guarded preflight and persist its success or failure snapshot."""
    if os.environ.get(REQUEST_ENV) != "1":
        raise PreflightBlocked(f"{REQUEST_ENV}=1 is required")
    assert_broker_access_allowed()
    request_id = request_id or f"LIVE-PREFLIGHT-{uuid.uuid4().hex[:12]}"
    diag = diagnostics_dir()
    diag.mkdir(parents=True, exist_ok=True)
    lock = diag / LOCK_NAME
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PreflightBlocked("PREFLIGHT_ALREADY_RUNNING") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"request_id": request_id, "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        api = None
        try:
            api = api_factory()
            snapshot = collect_read_only_preflight(api, product=product)
            quote_ok = all(c.get("passed") for c in snapshot["quote_subscription"])
            response: dict[str, Any] = {
                "schema_version": 1,
                "request_id": request_id,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True,
                "live_order_allowed": False,
                "snapshot": snapshot,
                "preflight": {"passed": quote_ok, "failed_checks": [] if quote_ok else ["QUOTE_SUBSCRIPTION_FAILED"]},
            }
        except Exception as exc:
            response = {
                "schema_version": 1,
                "request_id": request_id,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True,
                "live_order_allowed": False,
                "snapshot": None,
                "preflight": {"passed": False, "failed_checks": [f"{type(exc).__name__}: {exc}"]},
            }
        finally:
            if api is not None:
                try:
                    api.logout()
                except Exception:
                    pass
        _atomic_json(diag / f"broker_snapshot_{request_id}.json", response)
        _atomic_json(diag / "broker_snapshot_latest.json", response)
        return response
    finally:
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass
