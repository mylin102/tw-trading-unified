"""Three-state MTS position authority derived from the fills ledger.

Replaces the bool `has_position` authority with a ledger-derived three-state
view (FLAT / OPEN(trade_id, per-leg net qty) / UNKNOWN) plus pure gate
decisions for the pre-signal reset and the post-signal exit suppression.

Design contract (2026-08-06, P1):
- Pure functions (`project_fills`, `gate_decision_*`) are fully testable.
- `MtsLedgerProjection` updates incrementally (tail-read of new bytes /
  per-fill `apply_fill`) — never a per-tick full scan.
- The CURRENT trade is the latest trade_id with an open leg; leftover
  entries from older trades are anomalies and never protect the current
  trade.
- Invalid sides are never fabricated into LONG/SHORT (fail-closed).
- UNKNOWN (unreadable ledger) never triggers a strategy reset.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


class MtsAuthority(Enum):
    FLAT = "FLAT"
    OPEN = "OPEN"
    UNKNOWN = "UNKNOWN"


class MtsGateAction(Enum):
    RESET_STRATEGY = "RESET_STRATEGY"   # true divergence — force reset
    RECONSTRUCT = "RECONSTRUCT"         # state lags / strategy stale — rebuild from ledger
    PASS = "PASS"                       # nothing to do
    BLOCK_SIGNAL = "BLOCK_SIGNAL"       # post-signal: reject exit (authority flat)


@dataclass(frozen=True)
class MtsAuthorityState:
    status: MtsAuthority
    trade_id: Optional[str] = None
    near_qty: int = 0          # signed: +long / -short
    far_qty: int = 0
    near_side: Optional[str] = None   # "LONG"/"SHORT"/None — never fabricated
    far_side: Optional[str] = None
    near_entry: float = 0.0
    far_entry: float = 0.0
    current_trade_id: Optional[str] = None
    anomalies: tuple = ()


_VALID_SIDES = ("LONG", "SHORT")
_EXIT_FILL_TYPES = ("EXIT", "RELEASE", "COMBINED_EXIT", "COMBINED_EXIT_COMPLETED")


def _fill_leg(fill) -> Optional[str]:
    leg = str(fill.get("leg") or fill.get("contract") or "").upper()
    return leg if leg in ("NEAR", "FAR") else None


def _side_delta(fill) -> int:
    """Signed qty contribution. BUY/LONG → +1; SELL/SHORT → -1; garbage → 0.

    Convention: ENTRY fills carry the position side (SHORT/LONG); EXIT/RELEASE
    fills carry the closing order direction (BUY/SELL). Both accumulate on the
    same signed axis: closing a short IS a buy (+1).
    """
    try:
        side = str(fill.get("side") or "")
        qty = int(fill.get("qty") or 1)
    except (TypeError, ValueError):
        return 0
    if side in ("BUY", "LONG"):
        return qty
    if side in ("SELL", "SHORT"):
        return -qty
    return 0  # invalid side → 0, never a fabricated direction


def _fill_key(fill):
    return (
        fill.get("trade_id"),
        str(fill.get("leg") or fill.get("contract") or "").upper(),
        fill.get("side"),
        fill.get("price"),
        fill.get("timestamp"),
    )


def _reduce_trades(trades: dict, order: list) -> MtsAuthorityState:
    """Shared reduction over a per-trade per-leg accumulator.

    The CURRENT trade is the LATEST trade_id in the ledger. Leftover open
    legs from OLDER trades are anomalies — they never protect/define the
    current position (2026-08-06 P1 contract).
    """
    current = order[-1] if order else None
    if current is None:
        return MtsAuthorityState(MtsAuthority.FLAT)
    anomalies = tuple(
        tid for tid in order
        if tid != current and (trades[tid]["NEAR"] != 0 or trades[tid]["FAR"] != 0)
    )
    if trades[current]["NEAR"] == 0 and trades[current]["FAR"] == 0:
        return MtsAuthorityState(
            MtsAuthority.FLAT,
            current_trade_id=current,
            anomalies=anomalies,
        )
    nq, fq = trades[current]["NEAR"], trades[current]["FAR"]
    return MtsAuthorityState(
        MtsAuthority.OPEN,
        trade_id=current,
        near_qty=nq,
        far_qty=fq,
        near_side="LONG" if nq > 0 else ("SHORT" if nq < 0 else None),
        far_side="LONG" if fq > 0 else ("SHORT" if fq < 0 else None),
        near_entry=trades[current].get("near_entry", 0.0),
        far_entry=trades[current].get("far_entry", 0.0),
        current_trade_id=current,
        anomalies=anomalies,
    )


def project_fills(fills: Iterable[dict]) -> MtsAuthorityState:
    """Pure: reduce an iterable of fill dicts to the three-state authority."""
    trades: dict = {}
    order: list = []
    seen = set()
    for fill in fills:
        tid = fill.get("trade_id")
        if not tid:
            continue
        key = _fill_key(fill)
        if key in seen:
            continue  # duplicate deal — never double count
        seen.add(key)
        if tid not in trades:
            trades[tid] = {"NEAR": 0, "FAR": 0, "near_entry": 0.0, "far_entry": 0.0}
            order.append(tid)
        leg = _fill_leg(fill)
        if leg is None:
            continue
        ft = str(fill.get("fill_type") or "")
        d = _side_delta(fill)
        trades[tid][leg] += d
        if ft == "ENTRY" and d != 0 and trades[tid].get(f"{leg.lower()}_entry", 0.0) == 0.0:
            try:
                trades[tid][f"{leg.lower()}_entry"] = float(fill.get("price") or 0.0)
            except (TypeError, ValueError):
                pass
    return _reduce_trades(trades, order)


# ── pure gate decisions ──

def gate_decision_pre_signal(
    auth: MtsAuthorityState,
    state_has_pos: bool,
    strat_has_pos: bool,
    strat_trade_id: Optional[str],
) -> MtsGateAction:
    """Pre-signal gate: reset / reconstruct / pass before on_bar runs."""
    if strat_has_pos:
        if auth.status == MtsAuthority.FLAT:
            return MtsGateAction.RESET_STRATEGY   # true divergence
        if auth.status == MtsAuthority.OPEN:
            if auth.trade_id == strat_trade_id and state_has_pos:
                return MtsGateAction.PASS
            return MtsGateAction.RECONSTRUCT      # state lags or strategy stale
        return MtsGateAction.PASS                 # UNKNOWN — never reset
    else:
        if auth.status == MtsAuthority.OPEN:
            return MtsGateAction.RECONSTRUCT      # strategy lost the position
        return MtsGateAction.PASS


def gate_decision_post_signal(auth: MtsAuthorityState, signal_action: str) -> MtsGateAction:
    """Post-signal gate: suppress exit signals only when the ledger says FLAT."""
    if signal_action not in ("EXIT", "PARTIAL_EXIT"):
        return MtsGateAction.PASS
    if auth.status == MtsAuthority.FLAT:
        return MtsGateAction.BLOCK_SIGNAL
    return MtsGateAction.PASS   # OPEN (ledger confirms) or UNKNOWN (fail-open)


# ── incremental projection ──

class MtsLedgerProjection:
    """Incremental per-trade per-leg projection over the fills ledger.

    Bootstrap reads the whole file once; afterwards only NEW bytes are
    tail-read (`sync_from_ledger`) or fills are pushed directly
    (`apply_fill`). Never a per-tick full scan.
    """

    def __init__(self, path: Optional[str] = None, source: str = "PAPER"):
        self._path = path
        self.source = source          # "PAPER" | "LIVE" — authority paths stay separate
        self._trades: dict = {}
        self._order: list = []
        self._seen: deque = deque(maxlen=5000)   # recent fill keys (dedup)
        self._offset = 0
        self._unreadable = False

    def apply_fill(self, fill: dict) -> None:
        key = _fill_key(fill)
        if key in self._seen:
            return
        self._seen.append(key)
        tid = fill.get("trade_id")
        if not tid:
            return
        if tid not in self._trades:
            self._trades[tid] = {"NEAR": 0, "FAR": 0, "near_entry": 0.0, "far_entry": 0.0}
            self._order.append(tid)
        leg = _fill_leg(fill)
        if leg is None:
            return
        ft = str(fill.get("fill_type") or "")
        d = _side_delta(fill)
        self._trades[tid][leg] += d
        if ft == "ENTRY" and d != 0 and self._trades[tid].get(f"{leg.lower()}_entry", 0.0) == 0.0:
            try:
                self._trades[tid][f"{leg.lower()}_entry"] = float(fill.get("price") or 0.0)
            except (TypeError, ValueError):
                pass

    def sync_from_ledger(self, path: Optional[str] = None) -> int:
        """Incremental tail-read; returns the number of rows consumed."""
        path = path or self._path
        if not path or not os.path.exists(path):
            self._unreadable = True
            return 0
        try:
            size = os.path.getsize(path)
            if size < self._offset:
                self._offset = 0    # file rotated/truncated — re-read (dedup via _seen)
            n = 0
            with open(path) as f:
                f.seek(self._offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        fill = json.loads(line)
                    except Exception:
                        continue
                    self.apply_fill(fill)
                    n += 1
                self._offset = f.tell()
            self._unreadable = False
            return n
        except Exception:
            self._unreadable = True
            return 0

    def snapshot(self) -> MtsAuthorityState:
        if self._unreadable and not self._trades:
            return MtsAuthorityState(MtsAuthority.UNKNOWN)
        return _reduce_trades(self._trades, self._order)
