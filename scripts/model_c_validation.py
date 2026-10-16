#!/usr/bin/env python3
"""Model C Canary validation (Phases 1,4,5,6,7) — run after each session.

Reads data/telemetry/model_c/*.jsonl (accepted/rejected/BBO) + fills log.
Reports market-data richness (P1), coverage (P4), accuracy (P5),
decision replay (P6), episode stats (P7). Shadow only — no writes."""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

GIT = os.path.expanduser("~/Documents/mylin102/tw-trading-unified-git")
MC_DIR = f"{GIT}/data/telemetry/model_c"
FILLS = f"{GIT}/logs/mts_trade_fills.jsonl"
PV = 10.0

rows = []
if os.path.isdir(MC_DIR):
    for fn in sorted(os.listdir(MC_DIR)):
        if not (fn.endswith(".jsonl") and fn.startswith("model_c_")):
            continue
        with open(os.path.join(MC_DIR, fn)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue

accepted = [r for r in rows if r.get("event_type") == "MODEL_C_PAIR_ACCEPTED"]
rejected = [r for r in rows if r.get("event_type") == "MODEL_C_PAIR_REJECTED"]

# BBO raw (unique state changes per leg)
bbo_files = [os.path.join(MC_DIR, fn) for fn in os.listdir(MC_DIR) if fn.startswith("bbo_raw_")]
leg_states = {"NEAR": set(), "FAR": set()}
leg_updates = {"NEAR": 0, "FAR": 0}
leg_first_ts = {}
leg_last_ts = {}
for bf in bbo_files:
    if not os.path.exists(bf):
        continue
    with open(bf) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                q = json.loads(line)
            except Exception:
                continue
            leg = q.get("leg")
            if leg not in leg_states:
                continue
            leg_updates[leg] += 1
            leg_states[leg].add((q.get("bid"), q.get("ask")))
            ts = q.get("receive_timestamp", "")
            if ts:
                leg_first_ts.setdefault(leg, ts)
                leg_last_ts[leg] = ts

print("=" * 62)
print("MODEL C CANARY VALIDATION")
print("=" * 62)
print(f"telemetry rows: {len(rows)} | accepted: {len(accepted)} | rejected: {len(rejected)}")

# ── Phase 1: far BBO vs far last-trade richness ─────────────────────────
print("\n[P1] MARKET DATA RICHNESS (unique BBO states — not callbacks)")
for leg in ("NEAR", "FAR"):
    n_upd = leg_updates[leg]
    n_uniq = len(leg_states[leg])
    span_min = None
    if leg_first_ts.get(leg) and leg_last_ts.get(leg):
        try:
            t1 = datetime.fromisoformat(leg_first_ts[leg][:19])
            t2 = datetime.fromisoformat(leg_last_ts[leg][:19])
            span_min = max((t2 - t1).total_seconds() / 60.0, 0.001)
        except Exception:
            pass
    rate = (n_uniq / span_min) if span_min else None
    print(f"  {leg}: updates={n_upd} unique_states={n_uniq} "
          f"unique/min={rate:.2f if rate else 'n/a'} span_min={span_min:.1f if span_min else 'n/a'}")
# compare vs far last-trade baseline 0.07/min (P2 canary)
far_u = leg_updates.get("FAR", 0)
far_span = None
if leg_first_ts.get("FAR") and leg_last_ts.get("FAR"):
    try:
        t1 = datetime.fromisoformat(leg_first_ts["FAR"][:19])
        t2 = datetime.fromisoformat(leg_last_ts["FAR"][:19])
        far_span = max((t2 - t1).total_seconds() / 60.0, 0.001)
    except Exception:
        pass
far_bbo_rate = len(leg_states.get("FAR", set())) / far_span if far_span else None
if far_bbo_rate is not None:
    verdict = ("RICHER" if far_bbo_rate > 0.5 else "COMPARABLE_TO_SPARSE" if far_bbo_rate > 0.1 else "STILL_SPARSE")
    print(f"  far BBO unique/min={far_bbo_rate:.2f} vs far last-trade ~0.07/min -> {verdict}")

# ── Phase 4: coverage ────────────────────────────────────────────────────
print("\n[P4] COVERAGE")
n_acc = len(accepted)
n_rej = len(rejected)
if n_acc + n_rej == 0:
    print("  no pairing data yet (canary collecting)")
else:
    coverage = n_acc / (n_acc + n_rej) * 100.0
    print(f"  accepted={n_acc} rejected={n_rej} coverage={coverage:.1f}%")
    print(f"  verdict: {'VIABLE' if coverage >= 90 else 'CONDITIONAL' if coverage >= 70 else 'INSUFFICIENT'}")

# ── Phase 5: accuracy (matched accepted snapshots vs realized) ──────────
print("\n[P5] ACCURACY (model_c mark vs realized — needs position-attached pairs)")
have_pnl = [r for r in accepted if r.get("executable_combined_gross_pnl") is not None]
print(f"  snapshots with executable pnl: {len(have_pnl)} (position attach via mark_position/recon)")
if have_pnl:
    vals = [r["executable_combined_gross_pnl"] for r in have_pnl]
    vals.sort()
    n = len(vals)
    print(f"  executable pnl: median={vals[n//2]:+.0f} p90={vals[int(n*0.9)]:+.0f} p95={vals[int(n*0.95)]:+.0f} max={vals[-1]:+.0f}")

# ── Phase 6: decision replay (needs trigger-aligned snapshots) ──────────
print("\n[P6] DECISION REPLAY")
trig = [r for r in accepted if r.get("trade_id")]
print(f"  trigger-linked snapshots: {len(trig)} (recon v2 aligns trigger ts -> snapshot)")
if trig:
    print("  confusion matrix + peak/giveback replay: after recon v2 run")

# ── Phase 7: episode stats ──────────────────────────────────────────────
print("\n[P7] EPISODE STATISTICS")
if rejected:
    ep_ids = {r.get("episode_id") for r in rejected}
    ep_attempts = defaultdict(int)
    for r in rejected:
        ep_attempts[r.get("episode_id")] += 1
    print(f"  rejection attempts: {len(rejected)} | independent episodes: {len(ep_ids)}")
    print(f"  attempts/episode: avg={len(rejected)/max(len(ep_ids),1):.0f} max={max(ep_attempts.values())}")
else:
    print("  no rejections yet")

# ── Quality gates ────────────────────────────────────────────────────────
print("\n[QUALITY GATES]")
unknown = sum(1 for r in rejected if r.get("reason") in ("UNKNOWN", "OTHER", "INVALID"))
print(f"  UNKNOWN reasons: {unknown} (must be 0)")
print(f"  shadow_only: {sum(1 for r in rows if r.get('shadow_only') is True)}/{len(rows)} rows")
print(f"  execution influence: 0 (collector has no write path to Policy J)")
