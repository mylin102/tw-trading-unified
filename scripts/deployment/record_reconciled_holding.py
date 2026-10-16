#!/usr/bin/env python3
"""Record a broker-reconciled MTS holding into the execution context.

Why this exists: the in-process startup gate accepts a non-flat broker account
only when the context carries ``canonical_holding`` (or a legacy exit-only
capability) that matches the broker legs exactly.  Nothing wrote that field, so
a broker-held spread (live 2026-09-24: TMFJ6 Sell 1 / TMFK6 Buy 1, opened by the
system's own entry 40 s after the deploy) could never be adopted: the deploy and
every restart refused with GUARD_POSITION_NOT_FLAT while the app also could not
manage the position.

This command is the operator act that records broker truth: it validates the
running owner's canonical snapshot (freshness, source, scope, integrity digest,
exactly two opposite futures legs, no open orders) and writes the legs into
``ctx.canonical_holding`` atomically.  It NEVER changes the execution mode: the
context stays quarantined, and only the existing gates may promote it.

DRY-RUN by default; ``--apply`` performs the write and archives the previous
context first.  Exit codes: 0 ok/plan printed, 1 refusal, 2 usage/IO error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

SNAPSHOT_MAX_AGE_S = 600.0
ARTIFACT_REL = "exports/trades/live/diagnostics/broker_snapshot_canonical.json"


def _refuse(code: str, detail: str = "") -> int:
    print(f"phase=REFUSED code={code} {detail}".rstrip())
    return 1


def _relevant(rows):
    return [r for r in rows or []
            if isinstance(r, dict)
            and str(r.get("account", "") or "") in ("", "futures")]


def validate(snapshot_path: Path, ctx: dict) -> tuple[dict | None, str]:
    """Return (canonical_holding, "") or (None, refusal_code)."""
    try:
        raw = snapshot_path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError):
        return None, "CANONICAL_SNAPSHOT_UNREADABLE"
    if not isinstance(data, dict):
        return None, "CANONICAL_SNAPSHOT_INVALID"
    if str(ctx.get("requested_mode") or "") != "live":
        return None, "CTX_NOT_LIVE_INTENT"
    if data.get("source") != "live_broker" or data.get("scope") != "futopt":
        return None, "CANONICAL_SOURCE_INVALID"
    if (data.get("fetch_status") or {}).get("capture") != "OK" or any(
            data.get(k) for k in ("errors", "query_failures", "capture_error")):
        return None, "CANONICAL_QUERY_FAILED"
    captured = data.get("captured_at")
    if type(captured) is not int or not math.isfinite(captured):
        return None, "CANONICAL_TIMESTAMP_INVALID"
    age = time.time() - captured / 1000.0
    if age < 0 or age > SNAPSHOT_MAX_AGE_S:
        return None, "CANONICAL_STALE"
    digest = data.get("canonical_input_hash")
    expected = hashlib.sha256(json.dumps(
        dict(data, canonical_input_hash=None), sort_keys=True,
        ensure_ascii=False, default=str).encode()).hexdigest()
    if not digest or digest != expected:
        return None, "CANONICAL_HASH_MISMATCH"
    positions = _relevant(data.get("positions"))
    if len(positions) != 2:
        return None, "CANONICAL_NOT_A_SPREAD"
    if _relevant(data.get("open_orders")):
        return None, "CANONICAL_PENDING_ORDERS"
    legs = []
    for row in positions:
        direction = str(row.get("direction") or "").lower().replace("action.", "")
        side = {"sell": "SHORT", "short": "SHORT",
                "buy": "LONG", "long": "LONG"}.get(direction)
        qty, avg = row.get("quantity"), row.get("avg_cost")
        if (not str(row.get("code") or "") or side is None
                or type(qty) is not int or qty <= 0
                or isinstance(avg, bool) or not isinstance(avg, (int, float))
                or float(avg) <= 0):
            return None, "CANONICAL_LEG_IDENTITY_INVALID"
        legs.append({"code": str(row["code"]), "side": side,
                     "qty": int(qty), "avg_cost": float(avg)})
    if legs[0]["side"] == legs[1]["side"]:
        return None, "CANONICAL_LEGS_NOT_OPPOSED"
    legs.sort(key=lambda item: item["code"])
    return {"canonical_legs": legs, "source": "broker_reconciled_record",
            "captured_at": captured, "canonical_input_hash": digest}, ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runtime-dir", required=True)
    ap.add_argument("--snapshot", default=None,
                    help=f"canonical artifact (default <runtime>/{ARTIFACT_REL})")
    ap.add_argument("--apply", action="store_true",
                    help="perform the write (default: dry-run, nothing is written)")
    args = ap.parse_args(argv)

    runtime = Path(args.runtime_dir)
    ctx_path = runtime / "execution_context.json"
    snapshot = Path(args.snapshot) if args.snapshot else runtime / ARTIFACT_REL
    try:
        ctx_raw = ctx_path.read_text(encoding="utf-8")
        ctx = json.loads(ctx_raw)
    except (OSError, ValueError):
        return _refuse("CTX_UNREADABLE", str(ctx_path))
    if not isinstance(ctx, dict):
        return _refuse("CTX_INVALID")

    holding, code = validate(snapshot, ctx)
    if holding is None:
        return _refuse(code, f"snapshot={snapshot}")
    if isinstance(ctx.get("canonical_holding"), dict):
        return _refuse("ALREADY_RECORDED",
                       json.dumps(ctx["canonical_holding"], sort_keys=True)[:200])
    print("phase=PLAN holding="
          + json.dumps(holding, sort_keys=True, ensure_ascii=False))
    if not args.apply:
        print("DRY-RUN: nothing written (pass --apply to record broker truth)")
        return 0

    hist = runtime / "logs" / "context_history"
    hist.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    archive = hist / f"ctx_{hashlib.sha256(ctx_raw.encode()).hexdigest()[:16]}_{stamp}.json"
    archive.write_text(ctx_raw, encoding="utf-8")

    new_ctx = dict(ctx)
    new_ctx["canonical_holding"] = holding
    tmp = ctx_path.with_name(ctx_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(new_ctx, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, ctx_path)
    back = json.loads(ctx_path.read_text(encoding="utf-8"))
    assert back.get("canonical_holding") == holding
    assert back.get("effective_mode") == ctx.get("effective_mode")
    print(f"phase=RECORDED archived={archive} mode={back.get('effective_mode')} "
          f"legs={[leg['code'] for leg in holding['canonical_legs']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
