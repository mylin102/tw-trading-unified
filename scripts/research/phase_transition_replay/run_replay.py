"""Replay artifact runner — research-only (v6: git provenance + sync BBO).

The runner NEVER touches historical artifacts without explicit
authorization. ALL runnable parameters (M_economic, fee assumptions,
staleness, pair-skew bound, config version, classifier) resolve from the
committed pre-registration manifest via a REQUIRED --prereg selector —
there are NO value defaults on the CLI.

Git provenance gate (reproducibility, v6): the manifest records the repo
HEAD commit, the runner + preregistration file sha256 and the research
subtree dirty status. If the prereg/runner sources are untracked, modified
vs HEAD, or the research subtree is dirty, the runner REFUSES with zero
output — the selector cannot be proven against HEAD.

Modes:
- --dry-run: validate the input (schema + quality + pair sync), write the
  manifest, compute NO PnL. This is the ONLY accepted run mode while the
  engine is not implemented.
- --authorize without --dry-run: REFUSED (non-zero, zero output).
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts.research.phase_transition_replay import execution
from scripts.research.phase_transition_replay import preregistration
from scripts.research.phase_transition_replay import stream

MANIFEST_VERSION = "phase-transition-replay-manifest-v3"
QUALITY_GATE_TIER = "EXECUTABLE_BBO"   # incomplete/unsynchronized quotes
                                        # never produce usable two-leg PnL

ORDERING_FIELDS = ("source_event_seq", "exchange_ts", "recv_ts")
QUOTE_FIELDS = ("bid", "ask", "age_s", "close_action", "quote_exchange_ts")

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_REL = "scripts/research/phase_transition_replay/run_replay.py"
PREREG_REL = "scripts/research/phase_transition_replay/preregistration.py"
RESEARCH_SUBTREE = "scripts/research"


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(args):
    try:
        r = subprocess.run(["git"] + args, cwd=str(REPO_ROOT),
                           capture_output=True, text=True, timeout=15)
        return r
    except Exception:
        return None


def git_provenance():
    """(ok, provenance_dict | reason) — reproducibility gate inputs."""
    head = _git(["rev-parse", "HEAD"])
    if head is None or head.returncode != 0 or not head.stdout.strip():
        return False, {"reason": "repo HEAD unreadable"}
    status = _git(["status", "--porcelain", "--untracked-files=all",
                   RESEARCH_SUBTREE])
    if status is None or status.returncode != 0:
        return False, {"reason": "git status unreadable"}
    dirty = bool(status.stdout.strip())
    prov = {"repo_head": head.stdout.strip(),
            "dirty": dirty,
            "runner_sha256": _sha256_file(REPO_ROOT / RUNNER_REL),
            "prereg_sha256": _sha256_file(REPO_ROOT / PREREG_REL)}
    tracked = {}
    blob_matches = {}
    for name, rel in (("runner", RUNNER_REL), ("prereg", PREREG_REL)):
        lsf = _git(["ls-files", "--error-unmatch", "--", rel])
        tracked[name] = bool(lsf is not None and lsf.returncode == 0)
        tree = _git(["ls-tree", "HEAD", "--", rel])
        local = _git(["hash-object", str(REPO_ROOT / rel)])
        head_blob = (tree.stdout.split("\t")[0].split()[-1]
                     if tree is not None and tree.stdout.strip() else None)
        local_blob = (local.stdout.strip() if local is not None else None)
        blob_matches[name] = bool(head_blob and head_blob == local_blob)
    prov["runner_tracked"] = tracked["runner"]
    prov["prereg_tracked"] = tracked["prereg"]
    prov["runner_matches_head"] = blob_matches["runner"]
    prov["prereg_matches_head"] = blob_matches["prereg"]
    prov["research_subtree_dirty"] = dirty
    ok = (tracked["runner"] and tracked["prereg"]
          and blob_matches["runner"] and blob_matches["prereg"]
          and not dirty)
    return ok, prov


def _validate_event(ev):
    """Input schema validation — per-event; returns (ok, reason)."""
    for f in ORDERING_FIELDS:
        v = ev.get(f)
        if not isinstance(v, int) or v <= 0:
            return False, f"ordering field {f} missing/invalid: {v!r}"
    if not execution.validate_epoch_ms(ev.get("decision_ts_ms")):
        return False, (f"decision_ts_ms invalid epoch-ms: "
                       f"{ev.get('decision_ts_ms')!r}")
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


def _read_input_once(path):
    """Read the input bytes EXACTLY ONCE (P0-1 TOCTOU).
    The sha256 is computed over the SAME in-memory bytes that are parsed
    (json.loads from the bytes) — the pathname is NEVER reopened after
    hashing, so a replaced file cannot decouple the manifest hash from the
    parsed events.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (OSError, ValueError) as e:
        raise ValueError(f"input artifact unreadable: {path}: {e}") from e
    input_sha = hashlib.sha256(data).hexdigest()
    try:
        events = json.loads(data)
    except ValueError as e:
        raise ValueError(f"input artifact unparseable: {path}: {e}") from e
    if not isinstance(events, list):
        raise ValueError(f"input artifact must be a JSON list: {path}")
    return input_sha, events


def _exclusive_out_dir(path):
    """Exclusive fresh output directory (P0-2 evidence overwrite).
    Refuses when the path EXISTS (empty or not) — every run creates its
    own new directory; an existing manifest can never be overwritten.
    """
    p = Path(path)
    if p.exists():
        raise FileExistsError(f"out-dir already exists: {path}")
    p.mkdir(parents=True, exist_ok=False)
    return p


def _write_manifest_atomic(out_dir, manifest):
    """temp + fsync + atomic rename — never a partially-written manifest,
    never an overwrite of an existing manifest."""
    target = os.path.join(out_dir, "manifest.json")
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True,
                      ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, target)
        dir_fd = os.open(out_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _quality_censor(events, params):
    """Schema + quality + pair-sync censoring (never silent, never
    treated as computable)."""
    staleness = dict(params["staleness"])
    staleness["max_pair_skew_ms"] = params["max_pair_skew_ms"]
    censored = []
    kept = []
    for ev in events:
        ok, why = _validate_event(ev)
        if not ok:
            censored.append((ev, f"schema: {why}"))
            continue
        bounds = dict(staleness)
        bounds["decision_ts_ms"] = ev["decision_ts_ms"]
        r = execution.executable_prices(
            ev["quotes"], decision_ts=ev["decision_ts_ms"],
            staleness_bounds=bounds)
        if r["tier"] != QUALITY_GATE_TIER:
            censored.append((ev, "; ".join(r["reasons"]) or r["tier"]))
            continue
        kept.append((ev, r))
    return kept, censored


def main(argv=None):
    """Artifact runner (research-only, v6)."""
    p = argparse.ArgumentParser(prog="phase_transition_replay")
    p.add_argument("--input", required=True,
                   help="reconciled event-stream artifact (JSON list)")
    p.add_argument("--out-dir", required=True, help="manifest output dir")
    p.add_argument("--prereg", required=True,
                   choices=preregistration.prereg_ids(),
                   help="committed pre-registration selector (no value defaults)")
    p.add_argument("--dry-run", action="store_true",
                   help="validate input + write manifest, no PnL")
    p.add_argument("--authorize", action="store_true",
                   help="explicit authorization to run the engine")
    args = p.parse_args(argv)

    if not args.dry_run:
        # the engine is NOT implemented: any non-dry-run attempt is a
        # REFUSED, zero output — never a fake success
        print("REFUSED: engine not implemented; only --dry-run is accepted",
              file=sys.stderr)
        return 3

    prov_ok, provenance = git_provenance()
    if not prov_ok:
        print(f"REFUSED: git provenance not provable: "
              f"{provenance.get('reason', provenance)}", file=sys.stderr)
        return 5

    params = preregistration.preregistration(args.prereg)
    try:
        input_sha, events = _read_input_once(args.input)
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 4
    ordered, stream_hash, clock = stream.ordered_stream(
        events, clock_contract="immutable-global")
    kept, censored = _quality_censor(ordered, params)
    kept_records = []
    for ev, r in kept:
        near = r["prices"]["near"]
        far = r["prices"]["far"]
        skew = abs(near["quote_exchange_ts"] - far["quote_exchange_ts"])
        kept_records.append({
            "event_seq": ev["source_event_seq"],
            "decision_ts_ms": ev["decision_ts_ms"],
            "near_quote_exchange_ts": near["quote_exchange_ts"],
            "far_quote_exchange_ts": far["quote_exchange_ts"],
            "pair_skew_ms": skew,
            "max_pair_skew_ms": params["max_pair_skew_ms"],
            "near_age_s": near["age_s"],
            "far_age_s": far["age_s"],
        })
    try:
        _exclusive_out_dir(args.out_dir)
    except FileExistsError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 6
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "preregistration_id": args.prereg,
        "preregistration_sha": preregistration.prereg_sha(args.prereg),
        "git_provenance": provenance,
        "parameters": {
            "m_economic": params["m_economic"],
            "fee_assumption_id": params["fee_assumption_id"],
            "fee_assumptions": params["fee_assumptions"],
            "staleness": params["staleness"],
            "max_pair_skew_ms": params["max_pair_skew_ms"],
            "timestamp_unit": params["timestamp_unit"],
            "timestamp_validator_version": params[
                "timestamp_validator_version"],
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
        "kept_records": kept_records,
        "censored_reasons": [{"event_seq": e.get("source_event_seq"),
                              "reason": r} for e, r in censored],
        "dry_run": True,
        "engine_run": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_manifest_atomic(args.out_dir, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
