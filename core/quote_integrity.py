"""Quote Integrity Guard — canonical quote validation and routing.

P0b (2026-07-31 incident): far-month tick (TMFI6 @ 43822) was written into
NEAR caches via unconditional market_data writes, producing fake peaks and
fake PnL. This module:

- wraps every quote in a QuoteEnvelope (raw/normalized contract, expected
  leg, callback source, exchange/receive timestamps, receive sequence,
  subscription generation, live/snapshot source)
- decides a single destination (NEAR_CACHE / FAR_CACHE / REJECT) via
  QuoteDecision
- rejects stale-generation, contract-role-mismatch, invalid-value and
  out-of-order quotes with distinct taxonomy codes
- records routing anomalies (far quote attempting a near write) into
  anomalous_quotes.jsonl — normal far quotes are NEVER quarantined
- exposes counters readable from runtime state
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Taxonomy (single source of truth) ────────────────────────────────────
class RejectCode(str, Enum):
    VALID_NEAR_QUOTE = "VALID_NEAR_QUOTE"          # accepted, near destination
    VALID_FAR_QUOTE = "VALID_FAR_QUOTE"            # accepted, far destination
    ROUTING_CROSS_LEG_WRITE_BLOCKED = "ROUTING_CROSS_LEG_WRITE_BLOCKED"
    CONTRACT_ROLE_MISMATCH = "CONTRACT_ROLE_MISMATCH"
    STALE_GENERATION = "STALE_GENERATION"
    INVALID_QUOTE_VALUE = "INVALID_QUOTE_VALUE"
    OUT_OF_ORDER_QUOTE = "OUT_OF_ORDER_QUOTE"
    PAIR_NOT_SYNCHRONIZABLE = "PAIR_NOT_SYNCHRONIZABLE"


class Destination(str, Enum):
    NEAR_CACHE = "NEAR_CACHE"
    FAR_CACHE = "FAR_CACHE"
    NONE = "NONE"


@dataclass(frozen=True)
class QuoteEnvelope:
    """Immutable capture of one quote at the moment it enters the system."""
    raw_contract: str                # as delivered by the callback
    normalized_contract: str         # after normalize_contract_code
    expected_leg: Optional[str]      # "NEAR" / "FAR" / None (unknown)
    callback_source: str             # "shioaji_on_tick" / "snapshot" / "gca_fallback"
    exchange_timestamp: str          # tick.datetime (exchange clock)
    receive_timestamp: float         # time.time() at entry
    receive_sequence: int            # monotonic per-process counter
    subscription_generation: int     # bump on resubscribe/rollover
    source_kind: str                 # "live" / "snapshot"
    price: float                     # canonical price for routing decisions
    close: float
    bid: float
    ask: float


@dataclass(frozen=True)
class QuoteDecision:
    """Single destination decision. One and only one destination is chosen."""
    destination: Destination
    code: RejectCode
    reason: Optional[str] = None
    contract_code: Optional[str] = None   # populated for anomaly records
    target_slot: Optional[str] = None     # populated for routing blocks


def _is_finite(v: float) -> bool:
    return math.isfinite(v) and v > 0.0


class QuoteIntegrityGuard:
    """Validates and routes quotes. Pure logic — no monitor coupling."""

    def __init__(
        self,
        near_code: str,
        far_code: str,
        ticker: str,
        anomalous_quotes_path: str,
        generation: int = 1,
    ) -> None:
        self.near_code = near_code
        self.far_code = far_code
        self.ticker = ticker
        self.anomalous_quotes_path = anomalous_quotes_path
        self.generation = generation
        self._receive_seq = 0
        self._last_exchange_ts: dict[str, str] = {}   # per contract
        self._last_leg: dict[str, str] = {}           # per contract → leg
        self.stats: dict[str, int] = {
            "VALID_NEAR_QUOTE": 0,
            "VALID_FAR_QUOTE": 0,
            "ROUTING_CROSS_LEG_WRITE_BLOCKED": 0,
            "CONTRACT_ROLE_MISMATCH": 0,
            "STALE_GENERATION": 0,
            "INVALID_QUOTE_VALUE": 0,
            "OUT_OF_ORDER_QUOTE": 0,
            "PAIR_NOT_SYNCHRONIZABLE": 0,
            "REJECTED_TOTAL": 0,
            "ACCEPTED_TOTAL": 0,
            "QUOTES_SEEN": 0,
        }

    # ── public API ────────────────────────────────────────────────────────
    def decide(self, env: QuoteEnvelope) -> QuoteDecision:
        """Route one quote envelope. Never raises; always returns a decision."""
        self.stats["QUOTES_SEEN"] += 1

        # 1. invalid value — reject before anything else
        if not _is_finite(env.close) or not _is_finite(env.price):
            return self._reject(RejectCode.INVALID_QUOTE_VALUE, env,
                                f"close={env.close} price={env.price}")

        # 2. stale subscription generation
        if env.subscription_generation != self.generation:
            return self._reject(RejectCode.STALE_GENERATION, env,
                                f"env_gen={env.subscription_generation} current={self.generation}")

        # 3. contract role resolution
        code = env.normalized_contract
        if code == self.near_code:
            role = "NEAR"
        elif code == self.far_code:
            role = "FAR"
        else:
            return self._reject(RejectCode.CONTRACT_ROLE_MISMATCH, env,
                                f"code={code} near={self.near_code} far={self.far_code}")

        # 4. out-of-order quote (per contract, exchange timestamp)
        prev_ts = self._last_exchange_ts.get(code)
        if prev_ts is not None and env.exchange_timestamp and env.exchange_timestamp <= prev_ts:
            return self._reject(RejectCode.OUT_OF_ORDER_QUOTE, env,
                                f"ts={env.exchange_timestamp} prev={prev_ts}")
        self._last_exchange_ts[code] = env.exchange_timestamp

        # 5. expected-leg sanity (sticky per contract — role flip is an anomaly)
        prev_leg = self._last_leg.get(code)
        if prev_leg is not None and prev_leg != role:
            return self._reject(RejectCode.CONTRACT_ROLE_MISMATCH, env,
                                f"leg flip {prev_leg}->{role} for {code}")
        self._last_leg[code] = role

        # 6. route to the unique destination
        if role == "NEAR":
            self.stats["VALID_NEAR_QUOTE"] += 1
            self.stats["ACCEPTED_TOTAL"] += 1
            return QuoteDecision(Destination.NEAR_CACHE, RejectCode.VALID_NEAR_QUOTE,
                                 contract_code=code, target_slot=f"{self.ticker}_NEAR")
        self.stats["VALID_FAR_QUOTE"] += 1
        self.stats["ACCEPTED_TOTAL"] += 1
        return QuoteDecision(Destination.FAR_CACHE, RejectCode.VALID_FAR_QUOTE,
                             contract_code=code, target_slot=f"{self.ticker}_FAR")

    def record_routing_block(self, code: str, target_slot: str, reason: str = "") -> None:
        """A far quote ATTEMPTED to write a NEAR destination (or vice versa).
        This is the ONLY event that writes anomalous_quotes.jsonl."""
        self.stats["ROUTING_CROSS_LEG_WRITE_BLOCKED"] += 1
        self._append_anomaly({
            "ts": time.time(),
            "reason": RejectCode.ROUTING_CROSS_LEG_WRITE_BLOCKED.value,
            "contract_code": code,
            "target_slot": target_slot,
            "detail": reason,
        })

    def mark_pair_not_synchronizable(self, reason: str) -> None:
        self.stats["PAIR_NOT_SYNCHRONIZABLE"] += 1

    def to_dict(self) -> dict:
        return dict(self.stats)

    # ── internals ─────────────────────────────────────────────────────────
    def _reject(self, code: RejectCode, env: QuoteEnvelope, reason: str) -> QuoteDecision:
        self.stats[code.value] += 1
        self.stats["REJECTED_TOTAL"] += 1
        return QuoteDecision(Destination.NONE, code, reason=reason,
                             contract_code=env.normalized_contract)

    def _append_anomaly(self, rec: dict) -> None:
        try:
            with open(self.anomalous_quotes_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass  # non-fatal — stats still count
