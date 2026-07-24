#!/usr/bin/env python3
"""Generate soak summary from all health evidence runs."""
import json, glob, os, sys
from collections import Counter

def main():
    base = "exports/market_data/soak"
    soak_roots = sorted(glob.glob(os.path.join(base, "*/")))

    all_runs = []
    total_samples = 0
    first_time = None
    last_time = None
    all_statuses = Counter()
    all_reasons = Counter()
    all_gens = []
    all_errors = set()
    all_writer_fails = set()
    interval_diffs = []

    prev_last = None

    for d in soak_roots:
        name = os.path.basename(os.path.normpath(d))
        fp = os.path.join(d, "mxf_runtime_health.jsonl")
        if not os.path.exists(fp):
            continue
        with open(fp) as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            continue

        first = json.loads(lines[0])
        last = json.loads(lines[-1])

        statuses = Counter()
        reasons = Counter()
        gens = []
        errors = []
        wfails = []
        near_ages = []

        for line in lines:
            d2 = json.loads(line)
            rh = d2["runtime_health"]
            statuses[rh["status"]] += 1
            for r in rh.get("degraded_reasons", []):
                reasons[r] += 1
            gens.append(rh["collector_generation"])
            errors.append(rh.get("callback_error_count", 0))
            wfails.append(rh.get("writer_consecutive_failures", 0))
            near_ages.append(rh.get("near_tick_age_ms") or 0)

        run = {
            "name": name,
            "samples": len(lines),
            "first": first["sampled_at"][:19],
            "last": last["sampled_at"][:19],
            "gen_start": gens[0],
            "gen_end": gens[-1],
            "regressions": sum(1 for i in range(1, len(gens)) if gens[i] < gens[i-1]),
            "statuses": dict(statuses),
            "degraded_reasons": dict(reasons),
            "max_callback_err": max(errors),
            "max_writer_fail": max(wfails),
            "max_near_age_ms": max(near_ages),
            "last_status": last["runtime_health"]["status"],
        }
        all_runs.append(run)
        total_samples += len(lines)
        all_statuses += statuses
        all_reasons += reasons
        all_gens.extend(gens)
        all_errors.add(max(errors))
        all_writer_fails.add(max(wfails))

        if not first_time:
            first_time = run["first"]
        last_time = run["last"]

        if prev_last is not None:
            from datetime import datetime
            t1 = datetime.fromisoformat(prev_last)
            t2 = datetime.fromisoformat(run["first"])
            gap = (t2 - t1).total_seconds()
            if gap > 300:  # >5min gap = between restarts
                interval_diffs.append(gap)

        prev_last = run["last"]

    # Summary
    print("=== SOAK SUMMARY ===")
    print(f"Runs: {len(all_runs)}")
    print(f"Total samples: {total_samples}")
    print(f"Time range: {first_time} → {last_time}")
    print(f"Generation range: {min(all_gens)} → {max(all_gens)}")
    print(f"Generation regressions: {sum(r['regressions'] for r in all_runs)}")
    print(f"Max callback_error_count: {max(all_errors)}")
    print(f"Max writer_consecutive_failures: {max(all_writer_fails)}")
    print(f"Status counts: {dict(all_statuses)}")
    print()

    print("=== DEGRADED REASONS ===")
    for reason, count in sorted(all_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")
    print()

    print("=== RESTART INTERVALS (seconds) ===")
    if interval_diffs:
        from statistics import mean, stdev
        print(f"Mean: {mean(interval_diffs):.0f}s")
        print(f"Std:  {stdev(interval_diffs):.0f}s")
        print(f"Min:  {min(interval_diffs):.0f}s")
        print(f"Max:  {max(interval_diffs):.0f}s")
        for i, gap in enumerate(interval_diffs):
            h = gap / 60
            print(f"  Gap {i+1}: {gap:.0f}s ({h:.1f}min)")
    print()

    print("=== ALL RUNS ===")
    for r in all_runs:
        dur = r["samples"] * 30 / 60  # approximate minutes
        print(f"  {r['name']}: {r['samples']} samples, gen {r['gen_start']}→{r['gen_end']}, "
              f"statuses={r['statuses']}, exit={r['last_status']}")

if __name__ == "__main__":
    main()
