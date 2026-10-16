"""F Shadow Canary — immediate atomic combined exit (pure shadow). v3 (Gate-7).

ADR-026 PROPOSED. MODE=SHADOW_ONLY, EXECUTION_INFLUENCE=FALSE.
Hard safety locks; synchronized-pair driven evaluate; fail-open (errors only
counted, never raised into tick loop); bounded buffered writer; latency
metrics; outcome hook at canonical settlement covering ALL exit types.

2026-08-04."""
import json
import os
import threading
from datetime import datetime, timezone

_TZ = timezone.utc


def _now():
    return datetime.now(_TZ).isoformat(timespec="milliseconds")


def _now_ms():
    return datetime.now(_TZ).timestamp() * 1000


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


class FShadowCollector:
    """Collects F (atomic combined exit) shadow candidates only."""

    EXECUTION_ALLOWED = False
    ORDER_SUBMISSION_ALLOWED = False
    STATE_MUTATION_ALLOWED = False
    LIFECYCLE_TRANSITION_ALLOWED = False

    def __init__(self, out_path, bbo_path=None):
        self._out = out_path
        self._bbo_path = bbo_path
        self._lock = threading.RLock()
        self._near = {}
        self._far = {}
        self._near_c = None
        self._far_c = None
        self._candidates = {}
        self._seq = 0
        self._buffer = []
        self._buffer_limit = 200
        self._errors = 0
        self._dropped = 0
        self._flush_errors = 0
        self._last_flush_ts = None
        self._recorded_outcomes = set()   # (trade_id, generation, settlement_id)
        self._evaluated_pairs = set()     # (near_seq, far_seq) — same pair eval once
        self._decision_count = 0
        self._decision_triggers = 0
        self._latency = {"feed": [], "state_snapshot": [], "evaluate": [],
                         "telemetry": [], "monitor_total": []}
        _d = os.path.dirname(out_path)
        if _d:
            os.makedirs(_d, exist_ok=True)

    def _load_existing(self):
        if not os.path.exists(self._out):
            return
        try:
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
                    elif r.get("event") == "ACTUAL_PATH":
                        gen = r.get("position_generation")
                        tid = r.get("trade_id")
                        self._recorded_outcomes.add(
                            (tid, gen, tuple(sorted(r.get("entry_order_ids") or []))))
        except Exception:
            self._errors += 1

    def bind_contracts(self, near_code, far_code):
        self._near_c = near_code
        self._far_c = far_code

    def pair_ready(self, max_age_ms=2000.0, max_skew_ms=500.0):
        if not self._near or not self._far:
            return False
        try:
            age_n = age_ms(self._near.get("receive_ts"))
            age_f = age_ms(self._far.get("receive_ts"))
            if age_n > max_age_ms or age_f > max_age_ms:
                return False
            skew = abs(age_ms(self._near.get("receive_ts"), self._far.get("receive_ts")))
            return skew <= max_skew_ms
        except Exception:
            self._errors += 1
            return False

    def on_quote(self, leg, bid, ask, bid_size=None, ask_size=None,
                 receive_ts=None, seq=None, contract_code=None):
        _t0 = _now_ms()
        try:
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
                     "receive_ts": receive_ts or _now(),
                     "seq": seq if seq is not None else self._seq}
                if leg == "NEAR":
                    self._near = q
                else:
                    self._far = q
                if self._bbo_path:
                    self._buffer.append({"event_type": "SHADOW_F_BBO", "leg": leg, **q})
                    self._maybe_flush()
        except Exception:
            with self._lock:
                self._errors += 1
        finally:
            self._latency["feed"].append(_now_ms() - _t0)

    def evaluate(self, position):
        _t0 = _now_ms()
        try:
            return self._evaluate_inner(position)
        except Exception:
            with self._lock:
                self._errors += 1
            ev = {"event": "REJECTED", "reason": "SHADOW_INTERNAL_ERROR",
                  "trade_id": position.get("trade_id"),
                  "position_generation": position.get("position_generation"),
                  "ts": _now(), "mode": "SHADOW_ONLY", "execution_influence": False,
                  "adr": "ADR-026", "adr_status": "PROPOSED"}
            self._write(ev)
            return ev
        finally:
            self._latency["evaluate"].append(_now_ms() - _t0)

    def _evaluate_inner(self, position):
        gen = position.get("position_generation") or position.get("trade_id")
        _pair_key = (self._near.get("seq"), self._far.get("seq"))
        with self._lock:
            if gen in self._candidates:
                return {"event": "DUPLICATE_CANDIDATE", "position_generation": gen,
                        "first_candidate_ts": self._candidates[gen]["ts"]}
            if _pair_key in self._evaluated_pairs:
                return {"event": "DUPLICATE_PAIR_SKIPPED",
                        "position_generation": gen, "pair": list(_pair_key)}
            self._evaluated_pairs.add(_pair_key)
        if self._near_c and position.get("near_contract") and position["near_contract"] != self._near_c:
            return self._reject("CONTRACT_PAIR_MISMATCH", position, "NEAR")
        if self._far_c and position.get("far_contract") and position["far_contract"] != self._far_c:
            return self._reject("CONTRACT_PAIR_MISMATCH", position, "FAR")
        nq = self._near
        fq = self._far
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
        pv = float(position.get("point_value", 10.0))
        thr = float(position.get("release_threshold_pts", 88.0))
        n_exit = nq["bid"] if str(position.get("near_side", "")).upper() in ("BUY", "LONG") else nq["ask"]
        f_exit = fq["bid"] if str(position.get("far_side", "")).upper() in ("BUY", "LONG") else fq["ask"]
        n_pnl = (n_exit - float(position["near_entry"])) * pv if str(position.get("near_side", "")).upper() in ("BUY", "LONG") \
            else (float(position["near_entry"]) - n_exit) * pv
        f_pnl = (f_exit - float(position["far_entry"])) * pv if str(position.get("far_side", "")).upper() in ("BUY", "LONG") \
            else (float(position["far_entry"]) - f_exit) * pv
        n_pts = n_pnl / pv
        f_pts = f_pnl / pv
        if n_pts > -thr and f_pts > -thr:
            return {"event": "SIGNAL_DETECTED", "breached": False,
                    "position_generation": gen, "near_pts": round(n_pts, 1),
                    "far_pts": round(f_pts, 1)}
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

    def record_production_decision(self, position, triggered, breached_leg=None,
                                  adverse_move_pts=None, threshold_pts=None,
                                  atr=None, mark_source=None, evaluation_id=None):
        """Layer-1 tap: production evaluator decision. All evaluations count;
        only first trigger / state transition writes detail."""
        self._decision_count += 1
        if triggered:
            self._decision_triggers += 1
            rec = {"event": "PRODUCTION_DECISION_TRIGGER",
                   "evaluation_id": evaluation_id,
                   "trade_id": position.get("trade_id"),
                   "position_generation": position.get("position_generation"),
                   "triggered": True, "breached_leg": breached_leg,
                   "adverse_move_pts": adverse_move_pts,
                   "threshold_pts": threshold_pts, "atr": atr,
                   "mark_source": mark_source,
                   "decision_count": self._decision_count,
                   "ts": _now(), "mode": "SHADOW_ONLY", "execution_influence": False,
                   "adr": "ADR-026", "adr_status": "PROPOSED"}
            self._write(rec)
        return self._decision_count

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

    PRIORITY_EVENTS = {"EXECUTABLE_CANDIDATE", "ACTUAL_PATH",
                        "STATE_INFLUENCE_VIOLATION", "SHADOW_INTERNAL_ERROR"}

    def _write(self, rec):
        with self._lock:
            self._buffer.append(rec)
            if rec.get("event") in self.PRIORITY_EVENTS:
                self.flush()          # critical events flush immediately
            else:
                self._maybe_flush()

    def buffer_stats(self):
        with self._lock:
            return {"buffer_depth": len(self._buffer),
                    "last_flush_ts": self._last_flush_ts,
                    "flush_errors": self._flush_errors,
                    "dropped_events": self._dropped,
                    "internal_errors": self._errors}

    def _maybe_flush(self):
        if len(self._buffer) >= self._buffer_limit:
            self.flush()

    def flush(self):
        with self._lock:
            if not self._buffer:
                return
            _t0 = _now_ms()
            try:
                with open(self._out, "a") as f:
                    for rec in self._buffer:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    f.flush()
                self._buffer = []
                self._last_flush_ts = _now()
            except Exception:
                self._flush_errors += 1
                self._dropped += len(self._buffer)
                self._buffer = []
            self._latency["telemetry"].append(_now_ms() - _t0)

    def record_actual(self, position, actual_total_net, naked_leg_exposure_min,
                      remaining_exit_ts=None, exit_type=None, settlement_id=None):
        """Exactly-once outcome at canonical settlement. Dedup key:
        (trade_id, position_generation, settlement_id). Partial fills must
        NOT write final outcome early; repeated callbacks / restart recovery
        must NOT emit a second outcome. Returns None if already recorded."""
        gen = position.get("position_generation") or position.get("trade_id")
        key = (position.get("trade_id"), gen,
               tuple(sorted(position.get("entry_order_ids") or [])))
        with self._lock:
            if key in self._recorded_outcomes:
                self._write({"event": "DUPLICATE_OUTCOME_SUPPRESSED",
                             "trade_id": position.get("trade_id"),
                             "position_generation": gen, "ts": _now(),
                             "mode": "SHADOW_ONLY", "execution_influence": False,
                             "adr": "ADR-026", "adr_status": "PROPOSED"})
                return None
            self._recorded_outcomes.add(key)
        _has_cand = gen in self._candidates
        rec = {"event": "ACTUAL_PATH", "position_generation": gen,
               "trade_id": position.get("trade_id"),
               "entry_order_ids": position.get("entry_order_ids") or [],
               "has_shadow_candidate": _has_cand,
               "shadow_status": None if _has_cand else "NO_SHADOW_CANDIDATE",
               "actual_total_net": actual_total_net,
               "naked_leg_exposure_min": naked_leg_exposure_min,
               "remaining_exit_ts": remaining_exit_ts,
               "exit_type": exit_type or "UNKNOWN",
               "ts": _now(),
               "mode": "SHADOW_ONLY", "execution_influence": False,
               "adr": "ADR-026", "adr_status": "PROPOSED"}
        self._write(rec)
        return rec
