#!/usr/bin/env python3
"""P1-2 lower-boundary invariant fuzz.

Each generated stale state is turned into a release intent and sent through
ShioajiClient.place_order.  The broker call itself is stubbed at
_place_order_unchecked; OrderManager.create_order is deliberately not the
interception point.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from strategies.futures.squeeze_futures.data.shioaji_client import (
    AdapterOrderError, ShioajiClient,
)

FIELDS = [
    "has_position", "state", "trade_id", "lifecycle", "near_status",
    "near_side", "near_entry", "near_last", "far_status", "far_side",
    "far_entry", "far_last", "release_state", "released_leg",
    "remaining_leg", "remaining_side", "release_stop_points", "trail_pts",
    "trail_peak", "trail_nadir", "trail_side", "trail_stop_price",
    "trail_distance_points", "entry_spread_z", "current_spread_z",
    "spread_z", "total_upl", "total_realized_pnl", "manual_trade_status",
    "manual_order_filled", "manual_order_ts", "manual_order_type",
    "last_exit_type", "last_reason", "last_failed_at_ms",
    "naked_leg_exposure_min", "_pending_exit_intent", "atr", "initial_balance",
    "peak_net_exit_pnl_twd", "renko_status", "sweep_result", "sweep_strat_used",
    "sweep_trades", "schema_version", "state_revision", "session_id",
    "status", "reason", "near_qty", "far_qty", "near_symbol", "far_symbol",
    "position_epoch", "runtime_id", "writer_identity", "generation",
    "legs", "account",
]
VALUES = [None, 0, "", "OPEN", "CLOSED", "SHORT", "LONG", "RELEASED", True, False]
BROKERS = {
    "flat": [],
    "holds_near": [("TMFI6", "SHORT", 1)],
    "holds_far": [("TMFJ6", "LONG", 1)],
    "query_failed": None,
}


def _base():
    return {"has_position": False, "state": "FLAT", "near_side": None,
            "far_side": None, "near_status": "CLOSED", "far_status": "CLOSED",
            "released_leg": None}


def probe_intent(state, broker):
    client = ShioajiClient.__new__(ShioajiClient)
    client.api = SimpleNamespace()
    client.is_logged_in = True
    client._execution_context = SimpleNamespace(
        is_live_ready=lambda: True,
        assert_order_allowed=lambda *args, **kwargs: None,
    )
    captured = []
    client._place_order_unchecked = lambda *args, **kwargs: captured.append({
        "symbol": getattr(args[0], "code", None) if args else None,
        "side": args[1] if len(args) > 1 else kwargs.get("action"),
        "qty": args[2] if len(args) > 2 else kwargs.get("quantity"),
        "strategy": kwargs.get("intent_action"),
    }) or "FUZZ"
    rows = None if broker is None else [
        {"code": code, "side": side, "direction": side, "qty": qty,
         "quantity": qty} for code, side, qty in broker
    ]
    client._broker_snapshot_provider = lambda: {
        "source": "live_broker", "positions": rows,
        "fetch_status": {"capture": "FAIL" if broker is None else "OK"},
    }
    stale_claim = (state.get("near_side") in ("LONG", "SHORT")
                   or state.get("near_status") == "OPEN"
                   or state.get("released_leg") == "near")
    if stale_claim:
        side = str(state.get("near_side") or "LONG").upper()
        try:
            client.place_order(
                # The harness' near-leg broker fixture is TMFI6; far-leg is
                # TMFJ6.  Keep the probe aligned with that canonical mapping.
                SimpleNamespace(code="TMFI6"),
                "BUY" if side == "SHORT" else "SELL", 1,
                premise={"kind": "HOLD_LEG", "leg": "near", "side": side, "qty": 1},
                intent_action="RELEASE_NEAR",
            )
        except (AdapterOrderError, ValueError):
            pass
    return captured


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/mts_rootfix_landmines.json")
    args = parser.parse_args()
    cases = []
    base = _base()
    for field in FIELDS:
        for value in VALUES:
            mutated = dict(base)
            mutated[field] = value
            cases.append((field, value, mutated))
    landmines = []
    for name, value, state in cases:
        for broker_name, broker in BROKERS.items():
            intents = probe_intent(state, broker)
            if intents and (not broker or not any(
                    str(i.get("symbol", "")).startswith(str(code)[:5])
                    for i in intents for code, _, _ in broker)):
                landmines.append({"case": f"{name}={value!r}",
                                  "broker": broker_name, "intents": intents})
    result = {"cases_total": len(cases), "broker_states": list(BROKERS),
              "landmines": landmines,
              "verdict": "LANDMINES_FOUND" if landmines else "CLEAN"}
    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"cases={len(cases)} broker_states={len(BROKERS)} landmines={len(landmines)}")
    print(f"report={args.out}")
    return 1 if landmines else 0


if __name__ == "__main__":
    raise SystemExit(main())
