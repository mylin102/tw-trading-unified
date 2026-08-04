"""Model C Canary — Synchronized BBO Executable Marking (shadow only).

2026-08-03. Purely observational:
  execution_influence = false, order_influence = false,
  policy_threshold_influence = false, shadow_only = true.

Per-leg executable exit semantics:
  LONG  -> close SELL at bid
  SHORT -> close BUY  at ask

Pairing: quote-state snapshot (latest_near_bbo / latest_far_bbo), gated by
  max_quote_age_ms / max_pair_skew_ms / book validity (bid>0, ask>0, ask>=bid).

Episode tracking: identical stale quote + same reason does NOT create new
  episodes per fresh-leg tick (attempt-level inflation guard).

Model B contrast: freshness-controlled latest-snapshot mark (no BBO
  executable semantics) — computed alongside Model C for comparison.
"""
import json
import os
import threading
from datetime import datetime

MODEL_C_VERSION = "MODEL_C_V1"
# initial observational thresholds (not execution gates)
MAX_QUOTE_AGE_MS = 2000
MAX_PAIR_SKEW_MS = 500

REJECT_REASONS = (
    "NEAR_BBO_MISSING", "FAR_BBO_MISSING", "NEAR_STALE", "FAR_STALE",
    "BOTH_STALE", "PAIR_SKEW_EXCEEDED", "INVALID_NEAR_BOOK",
    "INVALID_FAR_BOOK", "TIMESTAMP_MISSING", "POSITION_STATE_INCOMPLETE",
)


def _now_iso():
    return datetime.now().isoformat()


def _ts_to_ms(ts_iso):
    """ISO ts -> epoch ms (naive local; receive timestamps are local)."""
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        return dt.timestamp() * 1000.0
    except Exception:
        return None


class ModelCCollector:
    def __init__(self, telemetry_path: str, max_quote_age_ms: int = MAX_QUOTE_AGE_MS,
                 max_pair_skew_ms: int = MAX_PAIR_SKEW_MS, bbo_raw_path: str | None = None):
        self._path = telemetry_path
        self._bbo_raw_path = bbo_raw_path
        self.max_age = max_quote_age_ms
        self.max_skew = max_pair_skew_ms
        self._lock = threading.Lock()
        self._bbo = {"NEAR": None, "FAR": None}
        self._episodes = {}   # key -> episode dict
        self._episode_seq = 0
        self.counters = {"pairing_attempts": 0, "accepted": 0, "rejected": 0,
                         "bbos": 0, "episodes": 0}
        self.latest_accepted = None   # last accepted pair snapshot (for trigger ref)
        self.latest_pair = None       # last attempted pair (accepted or rejected)
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    # ── quote ingest ────────────────────────────────────────────────────
    def on_quote(self, leg: str, bid, ask, bid_size=None, ask_size=None,
                 exchange_ts=None, receive_ts=None, seq=None,
                 contract_code=None, source=None, subscription_id=None):
        """Leg BBO update. Triggers a pairing attempt on either-leg update."""
        if leg not in ("NEAR", "FAR"):
            return
        with self._lock:
            self.counters["bbos"] += 1
            rts = receive_ts or _now_iso()
            self._bbo[leg] = {
                "leg": leg, "contract_code": contract_code,
                "bid": float(bid) if bid is not None else None,
                "ask": float(ask) if ask is not None else None,
                "bid_size": bid_size, "ask_size": ask_size,
                "exchange_timestamp": exchange_ts,
                "receive_timestamp": rts,
                "sequence_number": seq, "source": source,
                "subscription_id": subscription_id,
            }
            self._write_bbo_raw(self._bbo[leg])
            return self._try_pair(receive_ts=rts)

    # ── pairing ─────────────────────────────────────────────────────────
    def _try_pair(self, receive_ts=None):
        self.counters["pairing_attempts"] += 1
        near = self._bbo.get("NEAR")
        far = self._bbo.get("FAR")
        eval_rts = _now_iso()  # evaluation reference: now, not leg's own ts
        near_age = self._age_ms(near, eval_rts)
        far_age = self._age_ms(far, eval_rts)
        skew = self._skew_ms(near, far)
        exch_skew = self._exch_skew_ms(near, far)

        # reason resolution (ordered; explicit taxonomy, never UNKNOWN)
        reason, stale_leg = None, None
        if near is None:
            reason = "NEAR_BBO_MISSING"
        elif far is None:
            reason = "FAR_BBO_MISSING"
        elif near_age is None or far_age is None:
            reason = "TIMESTAMP_MISSING"
        elif near_age > self.max_age and far_age > self.max_age:
            reason, stale_leg = "BOTH_STALE", "BOTH"
        elif near_age > self.max_age:
            reason, stale_leg = "NEAR_STALE", "NEAR"
        elif far_age > self.max_age:
            reason, stale_leg = "FAR_STALE", "FAR"
        elif skew is not None and skew > self.max_skew:
            reason = "PAIR_SKEW_EXCEEDED"
        elif not self._book_valid(near):
            reason = "INVALID_NEAR_BOOK"
        elif not self._book_valid(far):
            reason = "INVALID_FAR_BOOK"
        else:
            reason = None

        if reason is not None:
            ep_key, ep_id, is_new = self._episode_for(reason, stale_leg, near, far)
            self.counters["rejected"] += 1
            rec = {
                "event_type": "MODEL_C_PAIR_REJECTED",
                "reason": reason, "stale_leg": stale_leg,
                "near_quote_age_ms": near_age, "far_quote_age_ms": far_age,
                "receive_pair_skew_ms": skew, "exchange_pair_skew_ms": exch_skew,
                "episode_id": ep_id, "is_new_episode": is_new,
                "attempt_count_in_episode": self._episodes[ep_key]["attempts"],
                "timestamp": _now_iso(), "model_version": MODEL_C_VERSION,
                "shadow_only": True,
            }
            self._write(rec)
            self.latest_pair = rec
            return rec

        # accepted
        self.counters["accepted"] += 1
        # episode resolution (previous rejection recovered)
        self._clear_episode_if_recovered()
        rec = {
            "event_type": "MODEL_C_PAIR_ACCEPTED",
            "trade_id": None,
            "near_contract": near.get("contract_code"),
            "far_contract": far.get("contract_code"),
            "near_bid": near["bid"], "near_ask": near["ask"],
            "far_bid": far["bid"], "far_ask": far["ask"],
            "near_exchange_ts": near.get("exchange_timestamp"),
            "far_exchange_ts": far.get("exchange_timestamp"),
            "near_receive_ts": near.get("receive_timestamp"),
            "far_receive_ts": far.get("receive_timestamp"),
            "near_quote_age_ms": near_age, "far_quote_age_ms": far_age,
            "receive_pair_skew_ms": skew, "exchange_pair_skew_ms": exch_skew,
            "near_position_side": None, "far_position_side": None,
            "near_entry_avg_price": None, "far_entry_avg_price": None,
            "near_open_qty": None, "far_open_qty": None,
            "near_point_value": None, "far_point_value": None,
            "near_executable_exit_price": None, "far_executable_exit_price": None,
            "near_executable_gross_pnl": None, "far_executable_gross_pnl": None,
            "executable_combined_gross_pnl": None,
            "model_b_mark_pnl": None, "model_b_to_model_c_gap": None,
            "model_version": MODEL_C_VERSION, "shadow_only": True,
            "timestamp": _now_iso(),
        }
        self._write(rec)
        self.latest_accepted = rec
        self.latest_pair = rec
        return rec

    def mark_position(self, near_side, far_side, near_entry, far_entry,
                      near_qty, far_qty, point_value=10.0, qty_source=None):
        """Attach position context to the latest accepted pair (shadow)."""
        with self._lock:
            if self.latest_accepted is None:
                return None
            rec = self.latest_accepted
            rec["near_position_side"] = near_side
            rec["far_position_side"] = far_side
            rec["near_entry_avg_price"] = near_entry
            rec["far_entry_avg_price"] = far_entry
            rec["near_open_qty"] = near_qty
            rec["far_open_qty"] = far_qty
            rec["near_point_value"] = point_value
            rec["far_point_value"] = point_value
            near_pnl = self._exec_pnl(near_side, near_entry, near_qty,
                                      rec["near_bid"], rec["near_ask"], point_value)
            far_pnl = self._exec_pnl(far_side, far_entry, far_qty,
                                     rec["far_bid"], rec["far_ask"], point_value)
            rec["near_executable_exit_price"] = (rec["near_bid"] if str(near_side).upper() == "LONG" else rec["near_ask"])
            rec["far_executable_exit_price"] = (rec["far_bid"] if str(far_side).upper() == "LONG" else rec["far_ask"])
            rec["near_executable_gross_pnl"] = near_pnl
            rec["far_executable_gross_pnl"] = far_pnl
            rec["executable_combined_gross_pnl"] = (
                (near_pnl if near_pnl is not None else 0.0) + (far_pnl if far_pnl is not None else 0.0)
            )
            if qty_source is not None:
                rec["qty_source"] = qty_source
            # accepted was ALREADY written (null position fields) — persist
            # an explicit update event so the JSONL carries full economics.
            self._write({**rec, "event_type": "MODEL_C_POSITION_MARKED",
                         "timestamp": _now_iso()})
            return rec

    @staticmethod
    def _exec_pnl(side, entry, qty, bid, ask, pv):
        if side is None or entry is None or qty is None:
            return None
        if str(side).upper() == "LONG":
            return (float(bid) - float(entry)) * int(qty) * float(pv)
        if str(side).upper() == "SHORT":
            return (float(entry) - float(ask)) * int(qty) * float(pv)
        return None

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _age_ms(quote, eval_rts):
        if quote is None:
            return None
        rt = _ts_to_ms(quote.get("receive_timestamp"))
        et = _ts_to_ms(eval_rts)
        if rt is None or et is None:
            return None
        return et - rt

    @staticmethod
    def _skew_ms(near, far):
        if near is None or far is None:
            return None
        nt = _ts_to_ms(near.get("receive_timestamp"))
        ft = _ts_to_ms(far.get("receive_timestamp"))
        if nt is None or ft is None:
            return None
        return abs(nt - ft)

    @staticmethod
    def _exch_skew_ms(near, far):
        if near is None or far is None:
            return None
        nt = _ts_to_ms(near.get("exchange_timestamp")) if near.get("exchange_timestamp") else None
        ft = _ts_to_ms(far.get("exchange_timestamp")) if far.get("exchange_timestamp") else None
        if nt is None or ft is None:
            return None
        return abs(nt - ft)

    @staticmethod
    def _book_valid(q):
        if q is None:
            return False
        b, a = q.get("bid"), q.get("ask")
        if b is None or a is None:
            return False
        return b > 0 and a > 0 and a >= b

    def _episode_for(self, reason, stale_leg, near, far):
        """Episode key: reason + stale quote identity. Same stale quote +
        same reason => attempt++, episode unchanged (inflation guard)."""
        stale_ts = None
        if stale_leg == "NEAR" and near:
            stale_ts = near.get("receive_timestamp")
        elif stale_leg == "FAR" and far:
            stale_ts = far.get("receive_timestamp")
        elif stale_leg == "BOTH":
            stale_ts = f"{near.get('receive_timestamp')}|{far.get('receive_timestamp')}"
        key = f"{reason}|{stale_leg}|{stale_ts}"
        if key not in self._episodes:
            self._episode_seq += 1
            self._episodes[key] = {"id": f"ep-{self._episode_seq:05d}", "attempts": 0}
            self.counters["episodes"] += 1
            is_new = True
        else:
            is_new = False
        self._episodes[key]["attempts"] += 1
        return key, self._episodes[key]["id"], is_new

    def _clear_episode_if_recovered(self):
        # recovery: any accepted pair resets stale episodes
        if self._episodes:
            self._episodes = {}

    def _write(self, rec):
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def _write_bbo_raw(self, quote):
        if not self._bbo_raw_path:
            return
        with open(self._bbo_raw_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event_type": "BBO_UPDATE", **quote}, default=str) + "\n")

    def snapshot(self):
        with self._lock:
            return {
                "bbo": {"NEAR": dict(self._bbo["NEAR"]) if self._bbo["NEAR"] else None,
                        "FAR": dict(self._bbo["FAR"]) if self._bbo["FAR"] else None},
                "counters": dict(self.counters),
                "latest_accepted": self.latest_accepted,
            }
