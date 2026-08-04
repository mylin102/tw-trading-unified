"""F Shadow Canary — immediate atomic combined exit (pure shadow).

ADR-026 PROPOSED. MODE=SHADOW_ONLY, EXECUTION_INFLUENCE=FALSE.
Hard safety locks: execution_allowed=False, order_submission_allowed=False,
state_mutation_allowed=False, lifecycle_transition_allowed=False.
No broker/paper API calls, no Exit Arbiter participation, independent
shadow_f.* namespace. Records three paths per eligible trade:
A actual production, B F shadow executable, C continuation marks.

2026-08-04."""
import json
import os
import threading
from datetime import datetime, timezone

_TZ = timezone.utc


def _now():
    return datetime.now(_TZ).isoformat(timespec="milliseconds")


class FShadowCollector:
    """Collects F (atomic combined exit) shadow candidates only."""

    # ── hard locks (asserted in tests via fault injection) ──────────────
    EXECUTION_ALLOWED = False
    ORDER_SUBMISSION_ALLOWED = False
    STATE_MUTATION_ALLOWED = False
    LIFECYCLE_TRANSITION_ALLOWED = False

    def __init__(self, out_path, bbo_path=None):
        self._out = out_path
        self._bbo_path = bbo_path
        self._lock = threading.RLock()
        self._near = {}   # contract_code -> {bid, ask, bid_size, ask_size, receive_ts, seq}
        self._far = {}
        self._near_c = None
        self._far_c = None
        self._candidates = {}   # position_generation -> first candidate (dedupe)
        self._seq = 0
        _d = os.path.dirname(out_path)
        if _d:
            os.makedirs(_d, exist_ok=True)

    def _load_existing(self):
        """Restart dedupe: rebuild first-candidate map from disk."""
        if not os.path.exists(self._out):
            return
        with open(self._out) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("event") == "EXECUTABLE_CANDIDATE":
                    gen = r.get("position_generation")
                    if gen and gen not in self._candidates:
                        self._candidates[gen] = {"ts": r.get("ts"), "candidate": r}

    # ── contract binding ────────────────────────────────────────────────
    def bind_contracts(self, near_code, far_code):
        self._near_c = near_code
        self._far_c = far_code

    # ── BBO feed (from monitor ticks — executable marks only) ───────────
    def on_quote(self, leg, bid, ask, bid_size=None, ask_size=None,
                 receive_ts=None, seq=None, contract_code=None):
        if contract_code and contract_code == self._near_c:
            leg = "NEAR"
        elif contract_code and contract_code == self._far_c:
            leg = "FAR"
        if leg not in ("NEAR", "FAR"):
            return
        if bid is None and ask is None:
            return
        with self._lock:
            self._seq += 1
            q = {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
                 "receive_ts": receive_ts or _now(), "seq": seq}
            if leg == "NEAR":
                self._near = q
            else:
                self._far = q
            if self._bbo_path:
                with open(self._bbo_path, "a") as f:
                    f.write(json.dumps({"event_type": "SHADOW_F_BBO", "leg": leg,
                                        **q}) + "\n")

    # ── candidate evaluation ────────────────────────────────────────────
    def evaluate(self, position):
        """position: {trade_id, position_generation, entry_order_ids,
        near_contract, far_contract, near_side, far_side, near_entry,
        far_entry, release_threshold_pts, atr, mark_source,
        production_release_leg, production_release_ts, point_value}
        Returns dict event (SIGNAL_DETECTED -> EXECUTABILITY_CHECK ->
        EXECUTABLE_CANDIDATE | REJECTED). Pure record — no side effects."""
        gen = position.get("position_generation") or position.get("trade_id")
        with self._lock:
            if gen in self._candidates:
                return {"event": "DUPLICATE_CANDIDATE", "position_generation": gen,
                        "first_candidate_ts": self._candidates[gen]["ts"]}
        if self._near_c and position.get("near_contract") and \
                position["near_contract"] != self._near_c:
            return self._reject("CONTRACT_PAIR_MISMATCH", position, "NEAR")
        if self._far_c and position.get("far_contract") and \
                position["far_contract"] != self._far_c:
            return self._reject("CONTRACT_PAIR_MISMATCH", position, "FAR")
        nq = self._near; fq = self._far
        if not nq:
            return self._reject("MISSING_NEAR_BBO", position, "NEAR")
        if not fq:
            return self._reject("MISSING_FAR_BBO", position, "FAR")
        now_ms = _now_ms()
        age_n = age_ms(nq["receive_ts"], now_ms)
        age_f = age_ms(fq["receive_ts"], now_ms)
        skew = abs(age_ms(nq["receive_ts"], fq["receive_ts"]))
        if age_n > 2000:
            return self._reject("STALE_NEAR", position, "NEAR", age_n)
        if age_f > 2000:
            return self._reject("STALE_FAR", position, "FAR", age_f)
        if skew > 500:
            return self._reject("PAIR_SKEW", position, None, skew=skew)
        if nq["bid"] is None or nq["ask"] is None or fq["bid"] is None or fq["ask"] is None:
            return self._reject("LOCKED_OR_CROSSED_MARKET", position, None)
        if nq["bid"] <= 0 or nq["ask"] <= 0 or nq["ask"] < nq["bid"] or \
                fq["bid"] <= 0 or fq["ask"] <= 0 or fq["ask"] < fq["bid"]:
            return self._reject("LOCKED_OR_CROSSED_MARKET", position, None)
        # executable breach: close LONG@bid, SHORT@ask
        pv = float(position.get("point_value", 10.0))
        thr = float(position.get("release_threshold_pts", 88.0))
        n_exit = nq["bid"] if str(position.get("near_side", "")).upper() in ("BUY", "LONG") else nq["ask"]
        f_exit = fq["bid"] if str(position.get("far_side", "")).upper() in ("BUY", "LONG") else fq["ask"]
        n_pnl = (n_exit - float(position["near_entry"])) * pv if str(position.get("near_side", "")).upper() in ("BUY", "LONG") \
            else (float(position["near_entry"]) - n_exit) * pv
        f_pnl = (f_exit - float(position["far_entry"])) * pv if str(position.get("far_side", "")).upper() in ("BUY", "LONG") \
            else (float(position["far_entry"]) - f_exit) * pv
        n_pts = n_pnl / pv; f_pts = f_pnl / pv
        if n_pts > -thr and f_pts > -thr:
            return {"event": "SIGNAL_DETECTED", "breached": False, "position_generation": gen,
                    "near_pts": round(n_pts, 1), "far_pts": round(f_pts, 1)}
        breached_leg = "NEAR" if n_pts <= -thr else "FAR"
        cand = {
            "event": "EXECUTABLE_CANDIDATE", "position_generation": gen,
            "trade_id": position.get("trade_id"),
            "entry_order_ids": position.get("entry_order_ids"),
            "near_contract": position.get("near_contract"), "far_contract": position.get("far_contract"),
            "breached_leg": breached_leg,
            "near_exit_side": "SELL" if str(position.get("near_side", "")).upper() in ("BUY", "LONG") else "BUY",
            "far_exit_side": "SELL" if str(position.get("far_side", "")).upper() in ("BUY", "LONG") else "BUY",
            "near_exit_bbo": n_exit, "far_exit_bbo": f_exit,
            "near_executable_pnl": round(n_pnl, 1), "far_executable_pnl": round(f_pnl, 1),
            "combined_executable_gross": round(n_pnl + f_pnl, 1),
            "near_quote_age_ms": round(age_n, 1), "far_quote_age_ms": round(age_f, 1),
            "pair_skew_ms": round(skew, 1),
            "release_threshold_pts": thr, "atr": position.get("atr"),
            "mark_source": position.get("mark_source"),
            "production_release_leg": position.get("production_release_leg"),
            "production_release_ts": position.get("production_release_ts"),
            "ts": _now(), "sequence": self._seq,
            "mode": "SHADOW_ONLY", "execution_influence": False,
            "adr": "ADR-026", "adr_status": "PROPOSED",
        }
        with self._lock:
            self._candidates[gen] = {"ts": cand["ts"], "candidate": cand}
            self._write(cand)
        return cand

    def _reject(self, reason, position, leg, age=None, skew=None):
        ev = {"event": "REJECTED", "reason": reason, "leg": leg,
              "position_generation": position.get("position_generation") or position.get("trade_id"),
              "trade_id": position.get("trade_id"), "ts": _now(),
              "mode": "SHADOW_ONLY", "execution_influence": False,
              "adr": "ADR-026", "adr_status": "PROPOSED"}
        if age is not None:
            ev["quote_age_ms"] = round(age, 1)
        if skew is not None:
            ev["pair_skew_ms"] = round(skew, 1)
        self._write(ev)
        return ev

    def _write(self, rec):
        with self._lock:
            with open(self._out, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ── production actual path (layer A) ────────────────────────────────
    def record_actual(self, position, actual_total_net, naked_leg_exposure_min,
                      remaining_exit_ts=None):
        gen = position.get("position_generation") or position.get("trade_id")
        rec = {"event": "ACTUAL_PATH", "position_generation": gen,
               "trade_id": position.get("trade_id"),
               "actual_total_net": actual_total_net,
               "naked_leg_exposure_min": naked_leg_exposure_min,
               "remaining_exit_ts": remaining_exit_ts, "ts": _now(),
               "mode": "SHADOW_ONLY", "execution_influence": False,
               "adr": "ADR-026", "adr_status": "PROPOSED"}
        self._write(rec)
        return rec


def _parse_ms(ts_iso):
    s = str(ts_iso).strip()
    for cand in (s, s[:26], s[:23]):
        try:
            t = datetime.fromisoformat(cand)
            if t.tzinfo is None:
                t = t.replace(tzinfo=_TZ)
            return t.timestamp() * 1000
        except Exception:
            continue
    return None


def age_ms(ts_iso, ref_ms=None):
    ms = _parse_ms(ts_iso)
    if ms is None:
        return float("inf")
    if ref_ms is None:
        return abs(_now_ms() - ms)
    if isinstance(ref_ms, str):
        r = _parse_ms(ref_ms)
        if r is None:
            return float("inf")
        ref_ms = r
    return abs(ref_ms - ms)


def _now_ms():
    return datetime.now(_TZ).timestamp() * 1000
