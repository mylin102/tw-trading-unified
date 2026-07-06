#!/usr/bin/env python3
"""
ADR-010 Sprint 6B: PM2 restart checkpoint utility.

Usage:
    python3 scripts/debug/oco_checkpoint.py capture [--label <name>]
    python3 scripts/debug/oco_checkpoint.py dump
    python3 scripts/debug/oco_checkpoint.py verify [--checkpoint <path>]
    python3 scripts/debug/oco_checkpoint.py list
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

MTS_STATE_FILE = os.getenv("MTS_STATE_PATH", "/tmp/mts_position_state.json")
CHECKPOINT_DIR = Path(__file__).resolve().parent.parent.parent / "_oco_checkpoints"


def _read_state(path: str = MTS_STATE_FILE) -> dict | None:
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def _lifecycle_block(state: dict) -> dict:
    lc = state.get("lifecycle", {})
    if not lc:
        return {}
    return lc


def _summary(state: dict) -> dict:
    if not state:
        return {"error": "no state"}
    lc = _lifecycle_block(state)
    rg = lc.get("release_group", {})
    tl = lc.get("trail_group", {})
    return {
        "has_position": state.get("has_position"),
        "state": state.get("state"),
        "phase": lc.get("phase"),
        "rg_status": rg.get("status"),
        "near_order_id": rg.get("near_order_id"),
        "far_order_id": rg.get("far_order_id"),
        "filled_leg": rg.get("filled_leg"),
        "filled_order_id": rg.get("filled_order_id"),
        "canceled_leg": rg.get("canceled_leg"),
        "sibling_cancel_status": rg.get("sibling_cancel_status"),
        "tl_status": tl.get("status"),
        "remaining_leg": tl.get("remaining_leg"),
        "exit_order_id": tl.get("exit_order_id"),
        "released_leg": state.get("released_leg"),
        "trade_id": state.get("trade_id"),
        "_updated": state.get("_updated"),
    }


def cmd_capture(args):
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    state = _read_state()
    if not state:
        print("❌ No MTS state file found — nothing to capture.")
        sys.exit(1)
    label = args.label or f"checkpoint"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{label}.json"
    path = CHECKPOINT_DIR / filename
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    print(f"✅ Captured checkpoint: {path}")
    s = _summary(state)
    print(f"   Lifecycle: {s}")
    return 0


def cmd_dump(args):
    state = _read_state()
    if not state:
        print("❌ No MTS state file found at:", MTS_STATE_FILE)
        sys.exit(1)
    s = _summary(state)
    print(json.dumps(s, indent=2))
    print("─" * 50)
    print("Full lifecycle block:")
    print(json.dumps(_lifecycle_block(state), indent=2, default=str))
    return 0


def cmd_verify(args):
    state = _read_state()
    if not state:
        print("❌ No MTS state file found at:", MTS_STATE_FILE)
        sys.exit(1)

    # Load checkpoint
    cp_path = args.checkpoint
    if not cp_path:
        cp_path = _find_latest_checkpoint()
    if not cp_path:
        print("❌ No checkpoint specified and none found in:", CHECKPOINT_DIR)
        sys.exit(1)

    with open(cp_path) as f:
        checkpoint = json.load(f)
    print(f"📋 Comparing against checkpoint: {cp_path}")

    current = _summary(state)
    expected = _summary(checkpoint)

    diffs = []
    for key in current:
        if key in ("_updated",):
            continue
        cv = str(current[key])
        ev = str(expected[key])
        if cv != ev and str(ev) != "None":
            diffs.append((key, ev, cv))

    if not diffs:
        print("✅ VERIFY PASSED — all expected fields preserved.")
    else:
        print("⚠️  VERIFY — differences found (current vs expected):")
        for key, ev, cv in diffs:
            print(f"   {key}: expected={ev}  current={cv}")
        print()
        print("Note: Some differences may be expected if the system advanced state.")
        print("Checkpoint captures state *at capture time*, not *at restart time*.")

    # Print current summary
    print()
    print("Current state summary:")
    print(json.dumps(current, indent=2))
    return 0 if not diffs else 1


def cmd_list(args):
    if not CHECKPOINT_DIR.exists():
        print("No checkpoints directory found.")
        return 0
    files = sorted(CHECKPOINT_DIR.iterdir(), reverse=True)
    if not files:
        print("No checkpoints captured yet.")
        return 0
    print(f"Checkpoints in {CHECKPOINT_DIR}:")
    for f in files:
        size = f.stat().st_size
        print(f"  {f.name}  ({size} bytes)")
    return 0


def _find_latest_checkpoint():
    if not CHECKPOINT_DIR.exists():
        return None
    files = sorted(CHECKPOINT_DIR.iterdir(), reverse=True)
    return str(files[0]) if files else None


def main():
    parser = argparse.ArgumentParser(description="OCO lifecycle checkpoint utility")
    parser.add_argument("action", choices=["capture", "dump", "verify", "list"])
    parser.add_argument("--label", type=str, default=None, help="Checkpoint label (capture)")
    parser.add_argument("--checkpoint", type=str, default=None, help="Path to checkpoint file (verify)")
    args = parser.parse_args()

    if args.action == "capture":
        return cmd_capture(args)
    elif args.action == "dump":
        return cmd_dump(args)
    elif args.action == "verify":
        return cmd_verify(args)
    elif args.action == "list":
        return cmd_list(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
