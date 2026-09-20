#!/usr/bin/env python3
"""Fresh read-only broker capture for the deployment gate (Collapse 1 / stage 4).

Produces the canonical flat-gate artifact via the sanctioned preflight entry
point (core.live_broker_preflight.run_once) — no manual values, no order path.
Exit codes: 0 = capture passed and the artifact exists; 5 = capture failed
(read-only preflight not passed or artifact missing).  Prints one JSON line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _emit(payload: dict, out_path: str) -> int:
    """Write the payload atomically; stdout keeps whatever the broker printed."""
    import os as _os
    tmp = f"{out_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        _os.fsync(fh.fileno())
    _os.replace(tmp, out_path)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True, help="credentials env file (broker keys)")
    ap.add_argument("--product", default="TMF")
    ap.add_argument("--request-id", default=None)
    ap.add_argument("--out", required=True,
                    help="file to write the JSON payload to (stdout carries broker banner noise)")
    args = ap.parse_args()

    # the read-only preflight refuses to run without this guard (by design)
    os.environ["LIVE_PREFLIGHT_NO_ORDERS"] = "1"

    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)

    import shioaji as sj
    from core.broker.shioaji_compat import safe_login
    from core.live_broker_preflight import diagnostics_dir, run_once

    def api_factory():
        api = sj.Shioaji()
        safe_login(api, os.environ["SHIOAJI_API_KEY"], os.environ["SHIOAJI_SECRET_KEY"],
                   contracts_timeout=10000)
        return api

    try:
        response = run_once(api_factory, request_id=args.request_id, product=args.product)
    except Exception as exc:  # fail-closed: no artifact => the deploy stops
        _emit({"preflight_passed": False, "artifact": None,
               "failed_checks": [f"{type(exc).__name__}: {exc}"]}, args.out)
        return 5

    artifact = diagnostics_dir() / "broker_snapshot_canonical.json"
    preflight = response.get("preflight") or {}
    out = {
        "preflight_passed": bool(preflight.get("passed")),
        "artifact": str(artifact) if artifact.is_file() else None,
        "failed_checks": list(preflight.get("failed_checks") or []),
    }
    if artifact.is_file():
        try:
            art = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            art = {}
        positions = art.get("positions") or []
        futures_rows = [p for p in positions
                        if isinstance(p, dict)
                        and str(p.get("account", "") or "") in ("", "futures")
                        and int(p.get("quantity", 0) or 0) != 0]
        out.update({
            "source": art.get("source"),
            "session_scope": art.get("session_scope"),
            "captured_at": art.get("captured_at"),
            "canonical_input_hash": art.get("canonical_input_hash"),
            "available_margin": art.get("available_margin"),
            "open_orders": len(art.get("open_orders") or []),
            "futures_open_positions": len(futures_rows),
            "stock_rows": sum(1 for p in positions if isinstance(p, dict)
                              and str(p.get("account", "") or "") == "stock"),
        })
    _emit(out, args.out)
    return 0 if out["preflight_passed"] and out["artifact"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
