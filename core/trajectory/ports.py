# 2026-07-17 Gemini CLI: Define clean Protocols (Ports) for sandbox dependency injection
from __future__ import annotations

from typing import Protocol, Sequence
from core.trajectory.contracts import TrajectoryEvent
from core.trajectory.state import ReplayState, OrderIntentSnapshot

class ClockPort(Protocol):
    @property
    def now_ns(self) -> int:
        ...

class OrderIntentSink(Protocol):
    def emit_intents(self, intents: Sequence[OrderIntentSnapshot]) -> None:
        ...

class StatePersistencePort(Protocol):
    def load_state(self, session_id: str, trade_id: str | None) -> ReplayState | None:
        ...
        
    def save_state(self, state: ReplayState) -> None:
        ...

class BrokerObservationPort(Protocol):
    def fetch_observations(self, event: TrajectoryEvent) -> dict[str, object]:
        ...
