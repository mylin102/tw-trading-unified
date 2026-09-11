#!/usr/bin/env python3
"""Sanctioned stale-context migration from one canonical broker capture."""

from __future__ import annotations

import argparse
import json

from core.reconciliation.startup_migration import migrate_stale_live_context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--tick-size", type=float, default=1.0)
    args = parser.parse_args()
    with open(args.snapshot, encoding="utf-8") as fh:
        snapshot = json.load(fh)
    ok, payload = migrate_stale_live_context(
        args.runtime_dir, snapshot, tick_size=args.tick_size)
    print(json.dumps({"ok": ok, "context": payload}, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
