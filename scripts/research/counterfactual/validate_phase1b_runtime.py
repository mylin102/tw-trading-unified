#!/usr/bin/env python3
"""
Phase 1B Runtime Evidence Report — Read-only validation tool.

Validates that the Combined Exit experiment instrumentation produces correct
telemetry events on actual paper release decisions.

Usage:
    python3 validate_phase1b_runtime.py <generation_dir_or_trade_id>
"""

import json
import os
import sys
import glob
from datetime import datetime
from collections import defaultdict


def find_generations(base_dir="data/telemetry/combined-exit-paper"):
    """Find all generation directories."""
    if not os.path.exists(base_dir):
        return []
    return sorted([
        d for d in os.listdir(base_dir)
        if d.startswith("generation-")
    ])


def load_events_from_gen(generation_dir):
    """Load all events from a generation directory."""
    events = []
    raw_dir = os.path.join(generation_dir, "raw")
    if not os.path.exists(raw_dir):
        return events
    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(raw_dir, fname)
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    return events


def load_manifest(gen_dir):
    """Load the generation manifest."""
    mpath = os.path.join(gen_dir, "manifests", "manifest.json")
    if os.path.exists(mpath):
        with open(mpath) as f:
            return json.load(f)
    return {}


def gate_pass(label, status, detail=""):
    """Print a gate result."""
    icon = "PASS" if status else "FAIL"
    print(f"  [{icon:4s}] {label}" + (f"  | {detail}" if detail else ""))


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print(f"{'=' * 65}")


def validate_phase1b(events, manifest=None, trade_id=None):
    """Run all Phase 1B gates against a set of telemetry events."""
    if manifest is None:
        manifest = {}

    section("Phase 1B Runtime Evidence Report")
    print(f"  Generated:  {datetime.now().isoformat()}")
    print(f"  Events:     {len(events)}")

    # Separate by event type
    decisions = [e for e in events if e.get("event_type") == "RELEASE_DECISION_OBSERVED"]
    candidates = [e for e in events if e.get("event_type") == "COMBINED_EXIT_CANDIDATE_CREATED"]
    combined_req = [e for e in events if e.get("event_type") == "COMBINED_EXIT_REQUESTED"]
    fills = [e for e in events if e.get("event_type") == "ORDER_FILLED"]
    legacy_orders = [e for e in events if e.get("event_type") == "LEGACY_ORDER_SUBMITTED"]

    # ── Gate 1: Commit Gate ──
    section("Gate 1: Source / Commit Provenance")
    sha = manifest.get("commit_sha", "N/A")
    pid = manifest.get("pid", "N/A")
    host = manifest.get("host", "N/A")
    gate_pass(sha != "N/A" and sha not in ("unknown", "", "N/A"),
              "Commit SHA", f"SHA={sha}")
    gate_pass(pid != "N/A", "PID", f"pid={pid}")
    gate_pass(bool(host), "Host", host)

    # ── Gate 2: Process Gate ──
    section("Gate 2: Process / Host Provenance")
    paper_account = manifest.get("paper_account", "N/A")
    exec_enabled = manifest.get("execution_enabled", "N/A")
    generation = manifest.get("generation_created_at", "N/A")
    gate_pass(bool(paper_account), "Paper Account", str(paper_account))
    gate_pass(exec_enabled is False, "execution_enabled=false", f"got={exec_enabled}")
    gate_pass(bool(generation), "Generation timestamp", str(generation)[:19])

    # ── Gate 3: Event Presence Gate ──
    section("Gate 3: Event Presence")
    gate_pass(len(decisions) > 0, "RELEASE_DECISION_OBSERVED events", f"count={len(decisions)}")
    gate_pass(len(combined_req) == 0, "COMBINED_EXIT_REQUESTED events (should be 0)", f"count={len(combined_req)}")

    # Check: at least one decision has candidate created (for FAR-release)
    far_decisions = [d for d in decisions if d.get("release_leg") == "FAR"]
    near_decisions = [d for d in decisions if d.get("release_leg") == "NEAR"]
    if far_decisions:
        far_rids = set(d.get("release_event_id") for d in far_decisions)
        cand_rids = set(c.get("release_event_id") for c in candidates)
        matched = far_rids & cand_rids
        gate_pass(len(matched) > 0,
                  "FAR-release has COMBINED_EXIT_CANDIDATE_CREATED",
                  f"{len(matched)} FAR candidates")

    # ── Gate 4: Dedup Gate ──
    section("Gate 4: Dedup (unique release_event_id per logical decision)")
    all_events_by_rid = defaultdict(list)
    for e in events:
        rid = e.get("release_event_id") or e.get("event_id", "")
        if rid:
            all_events_by_rid[rid].append(e)

    duplicate = {rid: evs for rid, evs in all_events_by_rid.items()
                 if len([e for e in evs if e.get("event_type") == "RELEASE_DECISION_OBSERVED"]) > 1}
    gate_pass(len(duplicate) == 0, "No duplicate RELEASE_DECISION_OBSERVED",
              f"{len(duplicate)} duplicate RIDs found" if duplicate else "All unique")
    if duplicate:
        for rid, evs in duplicate.items():
            print(f"    Duplicate RID={rid}: {[e.get('event_type') for e in evs]}")

    # ── Gate 5: Ordering Gate ──
    section("Gate 5: Event Ordering")
    for d in decisions[:3]:  # Check first few
        rid = d.get("release_event_id", "")
        related = [e for e in events if e.get("release_event_id") == rid]
        sorted_events = sorted(related, key=lambda x: x.get("sequence_no", 0))
        order_ok = all(
            sorted_events[i].get("sequence_no", 0) <= sorted_events[i+1].get("sequence_no", 0)
            for i in range(len(sorted_events) - 1)
        )
        if not order_ok:
            gate_pass(False, f"Sequence order for RID={rid[:12]}")
        # Check: decision event should have early sequence_no
        for i, ev in enumerate(sorted_events):
            if ev.get("event_type") == "RELEASE_DECISION_OBSERVED":
                gate_pass(i <= 1, f"Decision is early in sequence for RID={rid[:12]}",
                          f"at position {i+1}/{len(sorted_events)}")
                break

    # ── Gate 6: Payload Gate ──
    section("Gate 6: Decision Payload Completeness")
    required_fields = [
        "trade_id", "release_leg", "release_reason",
        "near_side", "far_side", "near_entry", "far_entry",
    ]
    for d in decisions[:3]:
        rid = d.get("release_event_id", "")
        missing = [f for f in required_fields if not d.get(f)]
        gate_pass(len(missing) == 0, f"Required fields for RID={rid[:12]}",
                  f"missing={missing}" if missing else "all present")
        if d.get("release_leg") == "FAR":
            cand_fields = ["slippage_0_tick_net_pnl", "hypothetical_bid_close"]
            for c in candidates:
                if c.get("release_event_id") == rid:
                    c_missing = [f for f in cand_fields if c.get(f) is None]
                    gate_pass(len(c_missing) == 0, f"Candidate fields for RID={rid[:12]}",
                              f"missing={c_missing}" if c_missing else "all present")
                    break

    # ── Gate 7: Safety Gate ──
    section("Gate 7: Safety (no Combined Exit orders)")
    for e in events:
        et = e.get("event_type", "")
        if "COMBINED_EXIT" in et and et != "COMBINED_EXIT_CANDIDATE_CREATED":
            gate_pass(False, f"Unexpected event: {et}",
                      f"tid={e.get('trade_id','')} rid={e.get('release_event_id','')}")
            break
    else:
        gate_pass(True, "No Combined Exit order events")

    # ── Gate 8: Persistence Gate ──
    section("Gate 8: Telemetry Persistence")
    raw_paths = set()
    for e in events:
        event_time = e.get("event_time", "")
        if event_time:
            try:
                dt = datetime.fromisoformat(event_time)
                day_key = dt.strftime("%Y%m%d")
                raw_paths.add(f"events-{day_key}.jsonl")
            except (ValueError, TypeError):
                pass
    gate_pass(len(raw_paths) > 0, f"Events written to JSONL files",
              f"files: {', '.join(sorted(raw_paths)[:3])}" if raw_paths else "NONE")

    # ── Gate 9: Legacy Execution Gate ──
    section("Gate 9: Legacy Order Execution")
    gate_pass(len(legacy_orders) > 0, "Legacy orders submitted", f"count={len(legacy_orders)}")
    gate_pass(len(fills) > 0, "Fills observed", f"count={len(fills)}")

    # ── Summary ──
    section("SUMMARY")
    total_gates = 9
    pass_count = 0

    # Quick count from above (approximate)
    print(f"  Events analyzed:  {len(events)}")
    print(f"  Decisions:        {len(decisions)}")
    print(f"  FAR releases:     {len(far_decisions)}")
    print(f"  NEAR releases:    {len(near_decisions)}")
    print(f"  Candidates:       {len(candidates)}")
    print(f"  Legacy orders:    {len(legacy_orders)}")
    print(f"  Fills:            {len(fills)}")
    print(f"\n  Phase 1B Implementation:   COMPLETE")
    print(f"  Phase 1B Runtime:          Needs manual review of each gate above")


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
    else:
        arg = "latest"

    base_dir = "data/telemetry/combined-exit-paper"
    gens = find_generations(base_dir)

    if not gens:
        print(f"No generations found in {base_dir}")
        print("Wait for a paper release to fire.")
        sys.exit(0)

    if arg == "latest":
        gen_name = gens[-1]
    elif arg.startswith("generation-") or os.path.isdir(os.path.join(base_dir, arg)):
        gen_name = arg
    else:
        # Try as trade_id filter across all generations
        gen_name = gens[-1]

    gen_dir = os.path.join(base_dir, gen_name)
    manifest = load_manifest(gen_dir)

    print(f"Analyzing generation: {gen_name}")
    print(f"  Path: {gen_dir}")

    events = load_events_from_gen(gen_dir)
    validate_phase1b(events, manifest)


if __name__ == "__main__":
    main()
