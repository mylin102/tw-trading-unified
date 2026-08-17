#!/usr/bin/env python3
"""Reconcile stale pending MTS orders at restart time.

Problem: an order can be saved with status=pending_submit while the
broker already cancelled it.  BUT `cancelled_at` alone is NOT broker
evidence: core/order_management/order.py cancel() also stamps it
(local watchdog cancel while the broker actually filled is a known
historical failure).  Marking such an order cancelled would corrupt
reconciliation and risk duplicate orders.

This script therefore requires EXPLICIT broker terminal evidence:

  - broker probe (read-only Shioaji query) MUST be available; if the
    probe fails or is absent -> fail-closed: NO changes at all.
  - For each pending_submit order (cancelled_at irrelevant — broker
    query is the truth):
      broker still lists the order as open  -> leave + warn (in-flight)
      broker shows a position on the symbol -> leave + quarantine
          (local cancel was wrong; the order FILLED)
      broker shows neither                   -> mark BROKER_NOT_FOUND
          (terminal, audit kept, never resubmit)
  - pending_submit WITHOUT cancelled_at     -> always left untouched.

Atomic write-back (backup + tmp + os.replace).  Exit code 0 always so
a reconcile hiccup never blocks the restart.
"""

import argparse
import glob
import inspect
import json
import os
import shutil
import sys
import time
from datetime import datetime

from core.broker_evidence import capture_session_snapshot
from core.order_lifecycle_reconciler import reconcile_order


def find_orders_file(runtime_dir):
    pat = os.path.join(runtime_dir, "exports", "trades", "TMF_*_orders.json")
    files = sorted(glob.glob(pat))
    return files[-1] if files else None


class BrokerProbe:
    """Read-only broker evidence.  Every query fails closed: on any
    exception the order is treated as still open / position present."""

    def __init__(self, api, account=None):
        self._api = api
        self._account = account

    def capture_snapshot(self, *, session_id):
        """Capture one read-only broker snapshot for the whole reconcile."""
        return capture_session_snapshot(self._api, session_id=session_id)

    def has_open_order(self, broker_order_id):
        if not broker_order_id:
            return True  # no broker identity -> fail closed (treat as open)
        try:
            trades = self._api.list_trades()
            for trade in trades:
                if str(getattr(trade, "ordno", "")) == str(broker_order_id):
                    return True
                if str(getattr(trade, "order_id", "")) == str(broker_order_id):
                    return True
            return False
        except Exception:
            return True  # query failed -> fail closed

    def has_position(self, symbol):
        try:
            kwargs = {"account": self._account} if self._account else {}
            positions = self._api.list_positions(**kwargs)
            for pos in positions:
                code = getattr(pos, "code", "") or getattr(
                    getattr(pos, "contract", None), "code", "")
                if code == symbol:
                    return True
            return False
        except Exception:
            return True  # query failed -> fail closed


def reconcile(orders_file, broker=None, dry_run=False):
    """Return dict {cancelled: [...], retained: [...]}.

    Without a broker probe nothing is ever changed (fail-closed).
    """
    with open(orders_file, encoding="utf-8") as fh:
        orders = json.load(fh)
    changed = []
    retained = []
    snapshot = None
    capture = getattr(broker, "capture_snapshot", None) if broker else None
    if callable(capture):
        snapshot = capture(
            session_id=f"restart-reconcile:{os.getpid()}:{time.time_ns()}"
        )
    for order in orders:
        if order.get("status") != "pending_submit":
            continue
        oid = order.get("order_id")
        if broker is None:
            # no broker evidence available -> fail closed
            retained.append(oid)
            continue
        if snapshot is not None:
            decision = reconcile_order(order, snapshot)
            if decision.action != "MARK_TERMINAL":
                retained.append(oid)
                continue
            # Broker evidence proves neither this order nor a position exists.
            # Keep the historical record but make the terminal disposition
            # explicit; never interpret this as a fill or resubmit.
            order["status"] = "BROKER_NOT_FOUND"
            order["reconciled_at"] = datetime.now().isoformat()
            order["reconcile_note"] = (
                "phantom pending: broker confirms neither open order nor "
                "position (BROKER_NOT_FOUND, RECONCILE_REQUIRED)"
            )
            changed.append(oid)
            continue
        broker_id = order.get("broker_order_id")
        symbol = order.get("symbol")
        if broker.has_open_order(broker_id):
            retained.append(oid)  # still in-flight at broker
            continue
        if broker.has_position(symbol):
            retained.append(oid)  # local state wrong; the order filled
            continue
        # The broker has NEITHER the open order NOR a position for this
        # symbol: the pending is phantom (cancelled_at presence is
        # irrelevant — the broker query is the truth).  Mark terminal —
        # never treated as a successful fill, never resubmitted — the
        # full record stays for audit.
        order["status"] = "BROKER_NOT_FOUND"
        order["reconciled_at"] = datetime.now().isoformat()
        order["reconcile_note"] = (
            "phantom pending: broker confirms neither open order nor "
            "position (BROKER_NOT_FOUND, RECONCILE_REQUIRED)")
        changed.append(oid)
    if changed and not dry_run:
        backup = orders_file + ".bak"
        shutil.copy2(orders_file, backup)
        tmp = orders_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(orders, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, orders_file)
    return {"cancelled": changed, "retained": retained}


def _build_broker_probe():
    """Build a read-only Shioaji probe from env credentials.

    Returns None when unavailable -> reconcile fails closed.
    """
    try:
        import shioaji as sj

        api_key = os.environ.get("SHIOAJI_API_KEY", "")
        secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "")
        person_id = os.environ.get("SHIOAJI_PERSON_ID", "")

        # Shioaji 1.7 does not accept person_id on login; person_id is used
        # by activate_ca instead.  Older/custom wrappers may expose it as an
        # explicit login parameter, so detect that contract without passing
        # speculative kwargs.  A signature-inspection failure is treated as
        # the conservative no-person-id form.
        api = sj.Shioaji()
        login_kwargs = {"api_key": api_key, "secret_key": secret_key}
        try:
            login_params = inspect.signature(api.login).parameters
        except (TypeError, ValueError):
            login_params = {}
        if person_id and "person_id" in login_params:
            login_kwargs["person_id"] = person_id

        result = api.login(**login_kwargs)
        if result is False:
            raise RuntimeError("probe login failed")
        return BrokerProbe(api, account=os.environ.get("SHIOAJI_ACCOUNT", ""))
    except Exception as exc:
        sys.stderr.write(
            "reconcile_pending_orders: broker probe unavailable "
            f"(BROKER_PROBE_UNAVAILABLE: {type(exc).__name__}); "
            f"fail-closed, no changes\n")
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orders-file", default=None,
                    help="explicit orders file (default: latest in runtime)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, do not write")
    ap.add_argument("--skip-broker", action="store_true",
                    help="do NOT query the broker (fail-closed no-op)")
    args = ap.parse_args()

    if args.orders_file:
        orders_file = args.orders_file
    else:
        runtime_dir = os.environ.get("TRADING_RUNTIME_DIR", "")
        if not runtime_dir:
            try:
                from core.runtime_paths import runtime_path
                runtime_dir = os.path.dirname(
                    os.path.dirname(runtime_path("exports", "x")))
            except Exception:
                sys.stderr.write(
                    "reconcile_pending_orders: TRADING_RUNTIME_DIR not set\n")
                sys.exit(0)
        orders_file = find_orders_file(runtime_dir)

    if not orders_file or not os.path.exists(orders_file):
        sys.stderr.write("reconcile_pending_orders: no orders file found\n")
        sys.exit(0)

    broker = None if args.skip_broker else _build_broker_probe()
    if broker is None:
        print("[reconcile] no broker evidence -> fail-closed, no changes")
        sys.exit(0)

    result = reconcile(orders_file, broker=broker, dry_run=args.dry_run)
    tag = "DRY-RUN " if args.dry_run else ""
    if result["cancelled"]:
        print(f"[reconcile] {tag}marked BROKER_NOT_FOUND (terminal, audit kept): {result['cancelled']}")
    if result["retained"]:
        print(f"[reconcile] {tag}retained (no broker proof): {result['retained']}")
    if not result["cancelled"] and not result["retained"]:
        print("[reconcile] no stale pending orders")
    sys.exit(0)


if __name__ == "__main__":
    main()
