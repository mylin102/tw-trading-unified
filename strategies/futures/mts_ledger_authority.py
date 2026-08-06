"""Fail-closed MTS position authority derived from local paper fills.

The fills ledger establishes *facts* (per-leg signed exposure); active orders
establish *intent* (a lifecycle transition).  They must not be conflated.
This module is deliberately independent of the monitor so its safety contract
can be exercised without a broker or a running strategy.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class MtsAuthority(Enum):
    FLAT = "FLAT"
    OPEN = "OPEN"
    TRANSITIONING = "TRANSITIONING"
    UNKNOWN = "UNKNOWN"


class MtsTransition(Enum):
    NONE = "NONE"
    ENTRY_BOTH_PENDING = "ENTRY_BOTH_PENDING"
    EXIT_BOTH_PENDING = "EXIT_BOTH_PENDING"
    RELEASE_SIBLING_PENDING = "RELEASE_SIBLING_PENDING"
    EXIT_REMAINING_PENDING = "EXIT_REMAINING_PENDING"
    EXIT_SETTLEMENT_PENDING = "EXIT_SETTLEMENT_PENDING"


class MtsGateAction(Enum):
    RESET_STRATEGY = "RESET_STRATEGY"
    RECONSTRUCT = "RECONSTRUCT"
    HOLD = "HOLD"                 # do not run normal on_bar lifecycle work
    PASS = "PASS"
    BLOCK_SIGNAL = "BLOCK_SIGNAL"


@dataclass(frozen=True)
class MtsAuthorityState:
    status: MtsAuthority
    trade_id: Optional[str] = None
    near_qty: int = 0
    far_qty: int = 0
    near_side: Optional[str] = None
    far_side: Optional[str] = None
    near_entry: float = 0.0
    far_entry: float = 0.0
    current_trade_id: Optional[str] = None
    transition: MtsTransition = MtsTransition.NONE
    snapshot_token: tuple = ()
    anomalies: tuple = ()


_VALID_SIDES = ("LONG", "SHORT")


def _fill_leg(fill: dict) -> Optional[str]:
    leg = str(fill.get("leg") or fill.get("contract") or "").upper()
    return leg if leg in ("NEAR", "FAR") else None


def _side_delta(fill: dict) -> Optional[int]:
    """Return signed quantity, or None for an invalid fact.

    Returning zero for malformed fields used to turn a corrupt open trade into
    FLAT.  That is unsafe, so invalid side/qty is an UNKNOWN authority.
    """
    try:
        qty = int(fill.get("qty") or 1)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None
    side = str(fill.get("side") or "").upper()
    if side in ("BUY", "LONG"):
        return qty
    if side in ("SELL", "SHORT"):
        return -qty
    return None


def _fill_key(fill: dict) -> tuple:
    """Prefer immutable order/deal identity; legacy fallback is conservative."""
    deal_id = fill.get("deal_id") or fill.get("order_id")
    if deal_id:
        return ("deal", str(deal_id))
    return (
        "legacy", fill.get("trade_id"), str(fill.get("leg") or fill.get("contract") or "").upper(),
        fill.get("side"), fill.get("price"), fill.get("qty"), fill.get("timestamp"),
    )


def _new_trade() -> dict:
    return {"NEAR": 0, "FAR": 0, "near_entry": 0.0, "far_entry": 0.0}


def _transition_status(exposure: MtsAuthorityState, transition: MtsTransition) -> MtsAuthorityState:
    if transition is MtsTransition.NONE:
        return exposure
    # The legal matrix deliberately permits the three orders of arrival seen
    # in paper callbacks.  All other combinations are not inferred.
    legal = {
        (MtsAuthority.FLAT, MtsTransition.ENTRY_BOTH_PENDING),
        (MtsAuthority.OPEN, MtsTransition.EXIT_BOTH_PENDING),
        (MtsAuthority.OPEN, MtsTransition.RELEASE_SIBLING_PENDING),
        (MtsAuthority.OPEN, MtsTransition.EXIT_REMAINING_PENDING),
        (MtsAuthority.FLAT, MtsTransition.EXIT_SETTLEMENT_PENDING),
        (MtsAuthority.OPEN, MtsTransition.EXIT_SETTLEMENT_PENDING),
    }
    # A partially filled entry/release is still OPEN exposure plus a pending
    # order.  It belongs to TRANSITIONING, not a guessed SPREAD/SINGLE_LEG.
    if (exposure.status, transition) in legal:
        return MtsAuthorityState(
            MtsAuthority.TRANSITIONING, trade_id=exposure.trade_id,
            near_qty=exposure.near_qty, far_qty=exposure.far_qty,
            near_side=exposure.near_side, far_side=exposure.far_side,
            near_entry=exposure.near_entry, far_entry=exposure.far_entry,
            current_trade_id=exposure.current_trade_id, transition=transition,
            snapshot_token=exposure.snapshot_token, anomalies=exposure.anomalies,
        )
    return MtsAuthorityState(
        MtsAuthority.UNKNOWN, current_trade_id=exposure.current_trade_id,
        transition=transition, snapshot_token=exposure.snapshot_token,
        anomalies=exposure.anomalies + ("ILLEGAL_TRANSITION_MATRIX",),
    )


def _reduce_trades(trades: dict, order: list, *, transition=MtsTransition.NONE, token=()) -> MtsAuthorityState:
    current = order[-1] if order else None
    if current is None:
        return _transition_status(MtsAuthorityState(MtsAuthority.FLAT, snapshot_token=token), transition)
    anomalies = tuple(tid for tid in order if tid != current and any(trades[tid][leg] for leg in ("NEAR", "FAR")))
    nq, fq = trades[current]["NEAR"], trades[current]["FAR"]
    if nq == 0 and fq == 0:
        state = MtsAuthorityState(MtsAuthority.FLAT, current_trade_id=current, snapshot_token=token, anomalies=anomalies)
    else:
        state = MtsAuthorityState(
            MtsAuthority.OPEN, trade_id=current, near_qty=nq, far_qty=fq,
            near_side="LONG" if nq > 0 else ("SHORT" if nq < 0 else None),
            far_side="LONG" if fq > 0 else ("SHORT" if fq < 0 else None),
            near_entry=trades[current]["near_entry"], far_entry=trades[current]["far_entry"],
            current_trade_id=current, snapshot_token=token, anomalies=anomalies,
        )
    return _transition_status(state, transition)


def project_fills(fills: Iterable[dict], *, transition: MtsTransition = MtsTransition.NONE) -> MtsAuthorityState:
    """Pure projection. Any malformed current-trade fact is UNKNOWN."""
    trades: dict = {}
    order: list = []
    seen = set()
    invalid = []
    for fill in fills:
        tid = fill.get("trade_id")
        if not tid:
            invalid.append("MISSING_TRADE_ID")
            continue
        key = _fill_key(fill)
        if key in seen:
            continue
        seen.add(key)
        if tid not in trades:
            trades[tid] = _new_trade()
            order.append(tid)
        leg, delta = _fill_leg(fill), _side_delta(fill)
        if leg is None or delta is None:
            invalid.append(f"INVALID_FILL:{tid}")
            continue
        trades[tid][leg] += delta
        if str(fill.get("fill_type") or "") == "ENTRY" and trades[tid][f"{leg.lower()}_entry"] == 0.0:
            try:
                trades[tid][f"{leg.lower()}_entry"] = float(fill.get("price") or 0.0)
            except (TypeError, ValueError):
                invalid.append(f"INVALID_PRICE:{tid}")
    if invalid and (not order or any(x.endswith(str(order[-1])) for x in invalid)):
        return MtsAuthorityState(MtsAuthority.UNKNOWN, current_trade_id=order[-1] if order else None, anomalies=tuple(invalid))
    return _reduce_trades(trades, order, transition=transition)


def gate_decision_pre_signal(auth: MtsAuthorityState, state_has_pos: bool, strat_has_pos: bool, strat_trade_id: Optional[str]) -> MtsGateAction:
    if auth.status is MtsAuthority.TRANSITIONING:
        return MtsGateAction.HOLD
    if auth.status is MtsAuthority.UNKNOWN:
        return MtsGateAction.HOLD
    if strat_has_pos:
        if auth.status is MtsAuthority.FLAT:
            return MtsGateAction.RESET_STRATEGY
        if auth.trade_id == strat_trade_id and state_has_pos:
            return MtsGateAction.PASS
        return MtsGateAction.RECONSTRUCT
    if auth.status is MtsAuthority.OPEN:
        return MtsGateAction.RECONSTRUCT
    return MtsGateAction.PASS


def gate_decision_post_signal(auth: MtsAuthorityState, signal_action: str, *, emergency: bool = False) -> MtsGateAction:
    if emergency:
        return MtsGateAction.PASS
    if signal_action not in ("EXIT", "PARTIAL_EXIT"):
        return MtsGateAction.PASS
    if auth.status in (MtsAuthority.FLAT, MtsAuthority.TRANSITIONING, MtsAuthority.UNKNOWN):
        return MtsGateAction.BLOCK_SIGNAL
    return MtsGateAction.PASS


class MtsLedgerProjection:
    """Event-driven ledger projection with rotation and partial-record safety.

    ``sync_from_ledger`` only opens the ledger when its identity, size, or mtime
    changed.  The exported token fences a decision against replacement,
    truncation, and append races.  LIVE intentionally reports UNKNOWN unless a
    broker adapter supplies an independently verified projection.
    """

    def __init__(self, path: Optional[str] = None, source: str = "PAPER"):
        self._path = path
        self.source = source.upper()
        self._trades: dict = {}
        self._order: list = []
        self._seen: deque = deque(maxlen=5000)
        self._offset = 0
        self._identity: tuple = ()
        self._mtime_ns = 0
        self._last_hash = ""
        self._tail = b""
        self._unreadable = False
        self._transition = MtsTransition.NONE
        self._broker_state: Optional[MtsAuthorityState] = None

    def set_transition(self, transition: MtsTransition) -> None:
        self._transition = transition

    def set_live_broker_state(self, state: MtsAuthorityState) -> None:
        """Live callers must inject a broker-verified state, never ledger truth."""
        self._broker_state = state

    def _reset(self) -> None:
        self._trades, self._order, self._seen = {}, [], deque(maxlen=5000)
        self._offset, self._tail, self._last_hash = 0, b"", ""

    def apply_fill(self, fill: dict) -> bool:
        key = _fill_key(fill)
        if key in self._seen:
            return False
        self._seen.append(key)
        tid, leg, delta = fill.get("trade_id"), _fill_leg(fill), _side_delta(fill)
        if not tid or leg is None or delta is None:
            self._unreadable = True
            return False
        if tid not in self._trades:
            self._trades[tid] = _new_trade()
            self._order.append(tid)
        self._trades[tid][leg] += delta
        if str(fill.get("fill_type") or "") == "ENTRY" and self._trades[tid][f"{leg.lower()}_entry"] == 0.0:
            self._trades[tid][f"{leg.lower()}_entry"] = float(fill.get("price") or 0.0)
        return True

    def sync_from_ledger(self, path: Optional[str] = None) -> int:
        path = path or self._path
        if not path:
            self._unreadable = True
            return 0
        try:
            stat = os.stat(path)
            identity = (stat.st_dev, stat.st_ino)
            changed = identity != self._identity or stat.st_size != self._offset or stat.st_mtime_ns != self._mtime_ns
            if not changed:
                return 0
            if identity != self._identity or stat.st_size < self._offset:
                self._reset()
                self._identity = identity
            with open(path, "rb") as f:
                f.seek(self._offset)
                data = self._tail + f.read()
                self._offset = f.tell()
            lines = data.splitlines(keepends=True)
            self._tail = b""
            if lines and not lines[-1].endswith(b"\n"):
                self._tail = lines.pop()
            count = 0
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                fill = json.loads(raw)
                if not self.apply_fill(fill):
                    self._unreadable = True
                    return count
                self._last_hash = hashlib.sha256(raw).hexdigest()
                count += 1
            self._mtime_ns = stat.st_mtime_ns
            self._unreadable = bool(self._tail)
            return count
        except Exception:
            self._unreadable = True
            return 0

    def snapshot(self) -> MtsAuthorityState:
        if self.source == "LIVE":
            return self._broker_state or MtsAuthorityState(MtsAuthority.UNKNOWN, anomalies=("LIVE_BROKER_SNAPSHOT_REQUIRED",))
        token = (self._identity, self._offset, self._last_hash)
        if self._unreadable:
            return MtsAuthorityState(MtsAuthority.UNKNOWN, current_trade_id=self._order[-1] if self._order else None, snapshot_token=token, anomalies=("LEDGER_UNREADABLE_OR_TRAILING_PARTIAL",))
        return _reduce_trades(self._trades, self._order, transition=self._transition, token=token)
