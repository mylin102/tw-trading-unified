# 2026-07-17 Gemini CLI: Implement Trajectory Replay Sandbox Kernel for PR 3A
from __future__ import annotations

import hashlib
import json
from dataclasses import replace, asdict
from typing import Sequence, List, Any

from core.trajectory.contracts import OrderedTrajectory, TrajectoryEvent, EventType
from core.trajectory.state import (
    ReplayState,
    MarketSnapshot,
    PositionSnapshot,
    LifecycleSnapshot,
)
from core.trajectory.clock import ReplayClock
from core.trajectory.trace import (
    BaselineReplayTrace,
    ReplayTraceFrame,
    BootstrapMetadata,
    EventConsumptionMode,
    DecisionSnapshot,
    TransitionSnapshot,
    compute_canonical_trace_hash,
)
from core.trajectory.sandbox_errors import InvariantViolationError

def compute_state_hash(state: ReplayState) -> str:
    """Compute a deterministic hash of the ReplayState for frame tracking."""
    # Round float values in state serialization to prevent repr diffs
    def float_normalizer(obj: Any) -> Any:
        if isinstance(obj, float):
            return round(obj, 6)
        if isinstance(obj, dict):
            return {k: float_normalizer(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [float_normalizer(x) for x in obj]
        return obj

    serialized = json.dumps(float_normalizer(asdict(state)), sort_keys=True, separators=(",", ":"))
    return hashlib.md5(serialized.encode("utf-8")).hexdigest()

class TrajectoryReplaySandbox:
    """
    Trajectory Replay Sandbox Kernel (PR 3A).
    Processes ordered events, advances virtual time, and runs the pure state reducer.
    """

    def __init__(self, clock: ReplayClock):
        self.clock = clock

    def replay_baseline(
        self,
        trajectory: OrderedTrajectory,
        initial_state: ReplayState,
    ) -> BaselineReplayTrace:
        """
        Run baseline replay on ordered trajectory.
        Computes trace frames, advances time, and validates immutability.
        """
        current_state = initial_state
        frames: List[ReplayTraceFrame] = []
        
        bootstrap_meta = BootstrapMetadata(
            mode="AUTHORITATIVE_OPENING_SNAPSHOT",
            initial_timestamp_ns=self.clock.now_ns,
            provenance={
                "strategy_name": "TMF_Calendar_Spread",
                "eligibility_policy_version": "v1.0-RELEASE-ONLY",
            }
        )

        for idx, event in enumerate(trajectory.events):
            # 1. Advance virtual clock (prevents timing regression)
            self.clock.advance_to(event.event_time_ns)

            # Capture state before modification
            state_before = current_state
            state_before_hash = compute_state_hash(state_before)

            # 2. Get consumption mode
            mode = self._get_consumption_mode(event.event_type)

            # 3. Apply state reducer based on consumption mode
            next_state = self._reduce_event(state_before, event, mode, idx)

            # Validate that state before was not mutated in place
            if state_before_hash != compute_state_hash(state_before):
                raise InvariantViolationError("State was mutated in place during event reduction!")

            state_after_hash = compute_state_hash(next_state)

            # Parse oracle decision/transition details if present for diagnostic matching
            decision_snap = None
            transition_snap = None
            if event.event_type == EventType.LIFECYCLE_TRANSITION:
                payload = event.payload
                transition_snap = TransitionSnapshot(
                    state_before=str(payload.get("state_before", "")),
                    state_after=str(payload.get("state_after", "")),
                    reason=str(payload.get("reason", "")),
                    timestamp_ns=event.event_time_ns,
                )

            # 4. Record trace frame
            frame = ReplayTraceFrame(
                event_index=idx,
                event_id=event.event_id,
                event_type=event.event_type,
                state_before_hash=state_before_hash,
                state_after_hash=state_after_hash,
                decision=decision_snap,
                order_intents=next_state.pending_intents,
                transitions=(transition_snap,) if transition_snap else (),
                consumed_as=mode,
                diagnostics=(),
            )
            frames.append(frame)

            # Move state forward
            current_state = next_state

        # Compute trace signature hash
        canonical_hash = compute_canonical_trace_hash(frames, current_state)

        return BaselineReplayTrace(
            bootstrap=bootstrap_meta,
            ordering_hash=trajectory.ordering_hash,
            frames=tuple(frames),
            final_state=current_state,
            canonical_trace_hash=canonical_hash,
        )

    def _get_consumption_mode(self, event_type: EventType) -> EventConsumptionMode:
        """
        Define the role of each event type according to baseline sandbox guidelines.
        """
        if event_type in (EventType.MARKET_TICK, EventType.SESSION_BOUNDARY, EventType.PROCESS_RESTART, EventType.BROKER_DISCONNECT, EventType.BROKER_RECONNECT):
            return EventConsumptionMode.INPUT
        elif event_type in (EventType.BROKER_FILL, EventType.BROKER_ACK):
            return EventConsumptionMode.INPUT_AND_ORACLE
        elif event_type in (EventType.LIFECYCLE_TRANSITION, EventType.STATE_RECONCILED):
            return EventConsumptionMode.ORACLE_ONLY
        return EventConsumptionMode.INPUT

    def _reduce_event(
        self,
        state: ReplayState,
        event: TrajectoryEvent,
        mode: EventConsumptionMode,
        index: int
    ) -> ReplayState:
        """
        Pure state reducer that computes the next state.
        No side-effects allowed.
        """
        if mode == EventConsumptionMode.ORACLE_ONLY:
            # Oracle events must not modify the state directly
            return replace(state, last_event_id=event.event_id, event_index=index)

        # Apply state changes for input events
        if event.event_type == EventType.MARKET_TICK:
            payload = event.payload
            symbol = str(payload.get("symbol", ""))
            last_price = float(payload.get("last_price", 0.0))
            bid_price = float(payload.get("bid_price", last_price))
            ask_price = float(payload.get("ask_price", last_price))

            # Update market snapshot
            new_prices = dict(state.market.last_prices)
            new_prices[symbol] = last_price
            new_bid_asks = dict(state.market.bid_asks)
            new_bid_asks[symbol] = (bid_price, ask_price)

            market_snap = MarketSnapshot(last_prices=new_prices, bid_asks=new_bid_asks)
            return replace(state, market=market_snap, last_event_id=event.event_id, event_index=index)

        elif event.event_type == EventType.BROKER_FILL:
            payload = event.payload
            symbol = str(payload.get("symbol", ""))
            side = str(payload.get("side", "BUY")).upper()
            price = float(payload.get("price", 0.0))
            qty = int(payload.get("quantity", 0))

            # Update position (INPUT_AND_ORACLE: updates position state)
            new_positions = []
            matched = False
            for pos in state.positions:
                if pos.symbol == symbol:
                    matched = True
                    delta = qty if side == "BUY" else -qty
                    new_qty = pos.qty + delta
                    # Recalculate avg_price if position size increases in the same direction
                    new_avg = pos.avg_price
                    if new_qty != 0 and (pos.qty * delta > 0):
                        new_avg = (pos.qty * pos.avg_price + delta * price) / new_qty
                    new_positions.append(
                        PositionSnapshot(
                            symbol=symbol,
                            qty=new_qty,
                            avg_price=new_avg,
                            unrealized_pnl=pos.unrealized_pnl,
                        )
                    )
                else:
                    new_positions.append(pos)

            if not matched:
                delta = qty if side == "BUY" else -qty
                new_positions.append(
                    PositionSnapshot(
                        symbol=symbol,
                        qty=delta,
                        avg_price=price,
                        unrealized_pnl=0.0,
                    )
                )

            return replace(
                state,
                positions=tuple(new_positions),
                last_event_id=event.event_id,
                event_index=index,
            )

        return replace(state, last_event_id=event.event_id, event_index=index)
