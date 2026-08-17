#!/usr/bin/env python3
"""Reconcile stale pending MTS orders at restart time.

Problem: an order can be saved with status=pending_submit while the
broker already cancelled it (cancelled_at stamped by the broker-cancel
path).  The local pending guard (_mts_has_pending_mts_orders) then
blocks new MTS entries forever even though the broker is flat.

This script runs before pm2 restart (wired into restart_live.sh):

  1. Find the latest TMF_*_orders.json in the trading runtime.
  2. For every order with status == pending_submit:
       - cancelled_at present  -> broker terminal evidence -> mark cancelled
       - cancelled_at absent   -> leave untouched (fail-closed: may be a
                                  real in-flight order)
  3. Atomic write-back (backup + tmp + os.replace).  Dry-run supported.

Exit code 0 always (warnings on stderr) so a reconcile hiccup never
blocks the restart; the pm2 start itself stays authoritative.
"""

import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime


def find_orders_file(runtime_dir):
    pat = os.path.join(runtime_dir, "exports", "trades", "TMF_*_orders.json")
    files = sorted(glob.glob(pat))
    return files[-1] if files else None


def reconcile(orders_file, dry_run=False):
    """Return the list of order_ids whose status was changed to cancelled."""
    with open(orders_file, encoding="utf-8") as fh:
        orders = json.load(fh)
    changed = []
    for order in orders:
        if order.get("status") == "pending_submit" and order.get("cancelled_at"):
            order["status"] = "cancelled"
            order["reconciled_at"] = datetime.now().isoformat()
            order["reconcile_note"] = "stale pending with broker cancel evidence"
            changed.append(order.get("order_id"))
    if changed and not dry_run:
        backup = orders_file + ".bak"
        shutil.copy2(orders_file, backup)
        tmp = orders_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(orders, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, orders_file)
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orders-file", default=None,
                    help="explicit orders file (default: latest in runtime)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, do not write")
    args = ap.parse_args()

    if args.orders_file:
        orders_file = args.orders_file
    else:
        runtime_dir = os.environ.get("TRADING_RUNTIME_DIR", "")
        if not runtime_dir:
            try:
                from core.runtime_paths import runtime_path
                runtime_dir = os.path.dirname(os.path.dirname(runtime_path("exports", "x")))
            except Exception:
                sys.stderr.write("reconcile_pending_orders: TRADING_RUNTIME_DIR not set\n")
                sys.exit(0)
        orders_file = find_orders_file(runtime_dir)

    if not orders_file or not os.path.exists(orders_file):
        sys.stderr.write("reconcile_pending_orders: no orders file found\n")
        sys.exit(0)

    changed = reconcile(orders_file, dry_run=args.dry_run)
    if changed:
        tag = "DRY-RUN " if args.dry_run else ""
        print(f"[reconcile] {tag}marked cancelled: {changed}")
    else:
        print("[reconcile] no stale pending orders")
    sys.exit(0)


if __name__ == "__main__":
    main()
