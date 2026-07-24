#!/usr/bin/env python3
"""Quick soak health check."""
import json, glob, os, sys

soak_roots = sorted(glob.glob("exports/market_data/soak/*/"))
if not soak_roots:
    print("No soak dirs found")
    sys.exit(0)

latest = soak_roots[-1]
filepath = os.path.join(latest, "mxf_runtime_health.jsonl")
if not os.path.exists(filepath):
    print(f"File not found: {filepath}")
    sys.exit(0)

with open(filepath) as f:
    lines = [l.strip() for l in f if l.strip()]

print(f"Samples: {len(lines)}")
print(f"File: {filepath}")

gens = []
for i, line in enumerate(lines):
    d = json.loads(line)
    rh = d["runtime_health"]
    gens.append(rh["collector_generation"])
    if i < 3 or i >= len(lines) - 3:
        print(f"  {i}: gen={rh['collector_generation']} near_age={rh.get('near_tick_age_ms')} status={rh['status']} reasons={rh['degraded_reasons']}")

regressions = sum(1 for i in range(1, len(gens)) if gens[i] < gens[i-1])
print(f"Regression: {regressions}")
print(f"Generation: {gens[0]} -> {gens[-1]}")
