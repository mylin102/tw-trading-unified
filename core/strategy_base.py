"""
StrategyBase — abstract base class for pluggable strategy plugins.

Every strategy plugin must subclass this and implement ``init()`` and
``on_bar()``.  Missing implementations fail at import time (ABC enforcement).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from core.signal import Signal
from core.strategy_context import StrategyContext


class StrategyBase(ABC):
    """Pluggable strategy interface.

    Lifecycle
    ---------
    1. ``init(context)`` — called once when strategy is activated
    2. ``on_bar(context)`` — called on each new bar, returns Signal or None
    3. ``cleanup()`` — called when strategy is deactivated (optional)
    """

    # ── Required Properties ──────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier matching the plugin filename.

        Examples: ``"counter_vwap"``, ``"spring_upthrust"``
        """
        ...

    @property
    def metadata(self) -> dict[str, Any]:
        """Strategy metadata for dashboard catalog and registry.

        Override to provide real values; defaults return a safe stub.
        """
        return {
            "asset_class": "futures",
            "version": "1.0",
            "backtest_pf": 0.0,
            "backtest_wr": 0.0,
            "backtest_maxdd": 0.0,
            "market_regime": "all",
            "description": "",
        }

    # ── Required Lifecycle Hooks ─────────────────────────────────────────

    @abstractmethod
    def init(self, context: StrategyContext) -> None:
        """Called once when strategy is activated.

        SDD Precondition:  ``context.position.size == 0`` (flat)
        SDD Postcondition: strategy internal state is initialized
        """
        ...

    @abstractmethod
    def on_bar(self, context: StrategyContext) -> Signal | None:
        """Called on each new bar.

        SDD Precondition:  ``context.market`` has valid OHLCV + indicators
        SDD Postcondition: returned Signal passes ``Signal.validate()``
        """
        ...

    # ── Optional Lifecycle Hooks ─────────────────────────────────────────

    def on_tick(self, tick: dict) -> None:
        """Called on each raw tick.  Default: no-op.

        Override for tick-level strategies (e.g. scalping, order-flow).
        """

    def cleanup(self) -> None:
        """Called when strategy is deactivated.  Default: no-op.

        Use to flush buffers, log a summary, release resources, etc.
        """

    # ── Config Validation ────────────────────────────────────────────────

    @property
    def config_schema(self) -> Any | None:
        """Optional Pydantic model for this strategy's config.

        Return ``None`` to skip schema-level validation (the registry
        will still perform basic structural checks).
        """
        return None
