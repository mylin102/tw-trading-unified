#!/usr/bin/env python3
"""
rebuild_trade_dataset.py — Nightly rebuild of trade dataset from JSONL audit logs.

Usage:
    python scripts/rebuild_trade_dataset.py
    python scripts/rebuild_trade_dataset.py --summary
    python scripts/rebuild_trade_dataset.py --idempotency   # rebuild twice, compare

Data flow: JSONL (audit log) → rebuild → generation dir → 'current' symlink
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# macOS Silicon optimization
if sys.platform == "darwin":
    import os
    os.system(f"taskpolicy -b -p {os.getpid()}")

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.trade_dataset import (
    BASE_DIR,
    EVENTS_LOG,
    FILLS_LOG,
    load_dataset,
    rebuild,
    summary,
)


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild trade dataset from JSONL audit logs"
    )
    parser.add_argument(
        "--fills", default=str(FILLS_LOG),
        help=f"Path to fills JSONL (default: {FILLS_LOG})",
    )
    parser.add_argument(
        "--events", default=str(EVENTS_LOG),
        help=f"Path to events JSONL (default: {EVENTS_LOG})",
    )
    parser.add_argument(
        "--output", default=str(BASE_DIR),
        help=f"Output directory (default: {BASE_DIR})",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--summary", action="store_true",
                        help="Print dataset summary and exit")
    parser.add_argument("--idempotency", action="store_true",
                        help="Rebuild twice and assert semantic equivalence")
    args = parser.parse_args()

    level = logging.ERROR if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")

    if args.summary:
        print(summary(Path(args.output)))
        return

    # First rebuild
    manifest = rebuild(
        fills_path=Path(args.fills),
        events_path=Path(args.events),
        output_dir=Path(args.output),
    )
    print()
    print(summary(Path(args.output)))

    # Idempotency check
    if args.idempotency:
        print("\n=== Idempotency check: rebuilding with same sources ===")
        import json as _json
        import pandas as _pd

        # Load first result
        ds1 = load_dataset(Path(args.output))
        if not ds1:
            print("FAIL: first rebuild produced no data")
            sys.exit(1)

        # Rebuild again (never pass the same build_id — force new generation)
        manifest2 = rebuild(
            fills_path=Path(args.fills),
            events_path=Path(args.events),
            output_dir=Path(args.output),
        )
        ds2 = load_dataset(Path(args.output))
        if not ds2:
            print("FAIL: second rebuild produced no data")
            sys.exit(1)

        # Semantic comparison
        errors = []
        for table in ["trade_facts", "trade_snapshots", "trade_decisions", "trade_outcomes"]:
            df1 = ds1[table]
            df2 = ds2[table]
            if df1.empty and df2.empty:
                continue
            # Sort by primary keys for deterministic comparison
            sort_cols = {
                "trade_facts": ["trade_id"],
                "trade_snapshots": ["trade_id", "snapshot_seq"],
                "trade_decisions": ["trade_id", "decision_seq"],
                "trade_outcomes": ["trade_id"],
            }
            keys = sort_cols.get(table, ["trade_id"])
            df1_sorted = df1.sort_values(keys).reset_index(drop=True)
            df2_sorted = df2.sort_values(keys).reset_index(drop=True)

            # Compare semantic content (exclude build-specific columns)
            exclude = {"dataset_build_id", "schema_version"}
            cols1 = [c for c in df1_sorted.columns if c not in exclude]
            cols2 = [c for c in df2_sorted.columns if c not in exclude]

            if cols1 != cols2:
                errors.append(f"{table}: column mismatch after excluding build columns")
                continue

            try:
                _pd.testing.assert_frame_equal(
                    df1_sorted[cols1], df2_sorted[cols2],
                    check_dtype=True,
                    check_like=True,
                )
                print(f"  {table}: ✓ semantic idempotent")
            except AssertionError as e:
                errors.append(f"{table}: {str(e)}")

        if errors:
            print(f"\nFAIL: {len(errors)} idempotency error(s)")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        else:
            print("\n✓ Semantic idempotency verified — all 4 tables match across rebuilds")


if __name__ == "__main__":
    main()
