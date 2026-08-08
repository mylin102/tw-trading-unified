"""Replay artifact runner — research-only (v4: dry-run acceptance).

The runner NEVER touches historical artifacts without explicit
authorization. --dry-run validates the reproducible input, applies
data-quality censoring and writes a manifest — no PnL is computed.
Without --dry-run or --authorize the runner refuses (exit 2).

Reproducible input: the input artifact's sha256 is recorded in the
manifest; the same input always yields the same stream hash.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone

MANIFEST_VERSION = "phase-transition-replay-manifest-v1"
QUALITY_GATE_TIER = "EXECUTABLE_BBO"   # incomplete quotes never produce
                                        # usable two-leg counterfactual PnL


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_events(path):
    """Load the reconciled event stream (JSON list of events)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"input artifact must be a JSON list: {path}")
    return data


def _quality_censor(events, execution, staleness_bounds):
    """Data-quality censoring: candidates with incomplete/stale quotes are
    censored WITH reason — never dropped silently."""
    censored = []
    kept = []
    for ev in events:
        quotes = ev.get("quotes")
        if not quotes:
            censored.append((ev, "no_quotes"))
            continue
        r = execution.executable_prices(
            quotes, decision_ts=ev.get("decision_ts"),
            staleness_bounds=staleness_bounds)
        if r["tier"] != QUALITY_GATE_TIER:
            censored.append((ev, "; ".join(r["reasons"]) or r["tier"]))
            continue
        kept.append(ev)
    return kept, censored


def main(argv=None):
    """Artifact runner (research-only)."""
    p = argparse.ArgumentParser(prog="phase_transition_replay")
    p.add_argument("--input", required=True,
                   help="reconciled event-stream artifact (JSON list)")
    p.add_argument("--out-dir", required=True, help="manifest output dir")
    p.add_argument("--dry-run", action="store_true",
                   help="validate input + write manifest, no PnL")
    p.add_argument("--authorize", action="store_true",
                   help="explicit authorization to run the engine")
    p.add_argument("--config-version", default="research-v1")
    p.add_argument("--fee-assumption-id", default="fee-v1")
    p.add_argument("--m-economic", type=float, default=25.0)
    args = p.parse_args(argv)

    if not args.dry_run and not args.authorize:
        print("REFUSED: --dry-run or --authorize required",
              file=sys.stderr)
        return 2

    import os
    from scripts.research.phase_transition_replay import execution
    from scripts.research.phase_transition_replay import stream
    from scripts.research.phase_transition_replay import pipeline

    input_sha = _sha256_file(args.input)
    events = _load_events(args.input)
    ordered, stream_hash, clock = stream.ordered_stream(
        events, clock_contract="immutable-global")
    staleness = {"max_age_s": 30}
    kept, censored = _quality_censor(ordered, execution, staleness)
    os.makedirs(args.out_dir, exist_ok=True)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "input_path": args.input,
        "input_sha256": input_sha,
        "stream_hash": stream_hash,
        "clock_contract": clock,
        "n_events": len(ordered),
        "n_kept": len(kept),
        "n_censored": len(censored),
        "censored_reasons": [{"event_seq": e.get("replay_seq"),
                              "reason": r} for e, r in censored],
        "config_version": args.config_version,
        "fee_assumption_id": args.fee_assumption_id,
        "m_economic": args.m_economic,
        "dry_run": bool(args.dry_run),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine_run": False,
    }
    with open(os.path.join(args.out_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
    if args.dry_run:
        return 0
    # engine run not implemented — the manifest records that no historical
    # artifact was executed
    return 0
