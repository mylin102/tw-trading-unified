"""Replay artifact runner — research-only (v5: preregistration-sourced).

The runner NEVER touches historical artifacts without explicit
authorization. ALL runnable parameters (M_economic, fee assumptions,
staleness, config version) resolve from the committed pre-registration
manifest (scripts/research/phase_transition_replay/preregistration.py) via
a REQUIRED --prereg selector — there are NO value defaults on the CLI.

Modes:
- --dry-run: validate the input (schema + quality), write the manifest,
  compute NO PnL. This is the ONLY accepted run mode while the engine is
  not implemented.
- --authorize without --dry-run: REFUSED (non-zero, zero output) — the
  engine is not implemented; fake success is never returned.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone

from scripts.research.phase_transition_replay import execution
from scripts.research.phase_transition_replay import pipeline
from scripts.research.phase_transition_replay import preregistration
from scripts.research.phase_transition_replay import stream

MANIFEST_VERSION = "phase-transition-replay-manifest-v2"
QUALITY_GATE_TIER = "EXECUTABLE_BBO"   # incomplete quotes never produce
                                        # usable two-leg counterfactual PnL

ORDERING_FIELDS = ("source_event_seq", "exchange_ts", "recv_ts")
QUOTE_FIELDS = ("bid", "ask", "age_s", "close_action")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_event(ev):
    """Input schema validation — per-event; returns (ok, reason).

    Requires ordering fields, a decision timestamp and the near/far quote
    shape. Invalid events are censored WITH reason — never silently valid.
    """
    for f in ORDERING_FIELDS:
        v = ev.get(f)
        if not isinstance(v, int) or v <= 0:
            return False, f"ordering field {f} missing/invalid: {v!r}"
    if not ev.get("decision_ts"):
        return False, "decision_ts missing"
    quotes = ev.get("quotes")
    if not isinstance(quotes, dict) or set(quotes) != {"near", "far"}:
        return False, f"quotes must be exactly near/far: {quotes!r}"
    for side in ("near", "far"):
        q = quotes[side]
        if not isinstance(q, dict):
            return False, f"{side} quote not a dict: {q!r}"
        for f in QUOTE_FIELDS:
            if f not in q:
                return False, f"{side} quote missing field {f}"
    return True, None


def _load_events(path):
    """Load the reconciled event stream; whole-file problems REFUSE."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"input artifact unreadable: {path}: {e}") from e
    if not isinstance(data, list):
        raise ValueError(f"input artifact must be a JSON list: {path}")
    return data


def _quality_censor(events, params):
    """Schema + quality censoring: invalid/incomplete events are censored
    WITH reason — never dropped silently, never treated as computable."""
    staleness = params["staleness"]
    censored = []
    kept = []
    for ev in events:
        ok, why = _validate_event(ev)
        if not ok:
            censored.append((ev, f"schema: {why}"))
            continue
        r = execution.executable_prices(
            ev["quotes"], decision_ts=ev.get("decision_ts"),
            staleness_bounds=staleness)
        if r["tier"] != QUALITY_GATE_TIER:
            censored.append((ev, "; ".join(r["reasons"]) or r["tier"]))
            continue
        kept.append(ev)
    return kept, censored


def main(argv=None):
    """Artifact runner (research-only, v5)."""
    p = argparse.ArgumentParser(prog="phase_transition_replay")
    p.add_argument("--input", required=True,
                   help="reconciled event-stream artifact (JSON list)")
    p.add_argument("--out-dir", required=True, help="manifest output dir")
    p.add_argument("--prereg", required=True, choices=preregistration.prereg_ids(),
                   help="committed pre-registration selector (no value defaults)")
    p.add_argument("--dry-run", action="store_true",
                   help="validate input + write manifest, no PnL")
    p.add_argument("--authorize", action="store_true",
                   help="explicit authorization to run the engine")
    args = p.parse_args(argv)

    params = preregistration.preregistration(args.prereg)

    if not args.dry_run:
        # the engine is NOT implemented: any non-dry-run attempt is a
        # REFUSED, zero output — never a fake success
        print("REFUSED: engine not implemented; only --dry-run is accepted",
              file=sys.stderr)
        return 3

    import os
    input_sha = _sha256_file(args.input)
    try:
        events = _load_events(args.input)
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 4
    ordered, stream_hash, clock = stream.ordered_stream(
        events, clock_contract="immutable-global")
    kept, censored = _quality_censor(ordered, params)
    os.makedirs(args.out_dir, exist_ok=True)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "preregistration_id": args.prereg,
        "preregistration_sha": preregistration.prereg_sha(args.prereg),
        "parameters": {
            "m_economic": params["m_economic"],
            "fee_assumption_id": params["fee_assumption_id"],
            "fee_assumptions": params["fee_assumptions"],
            "staleness": params["staleness"],
            "config_version": params["config_version"],
            "classifier": params["classifier"],
        },
        "input_path": args.input,
        "input_sha256": input_sha,
        "stream_hash": stream_hash,
        "clock_contract": clock,
        "n_events": len(ordered),
        "n_kept": len(kept),
        "n_censored": len(censored),
        "censored_reasons": [{"event_seq": e.get("source_event_seq"),
                              "reason": r} for e, r in censored],
        "dry_run": True,
        "engine_run": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(args.out_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
    return 0
