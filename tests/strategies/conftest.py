"""conftest for strategies tests."""
import builtins
import sys
from pathlib import Path

# Add options_engine path so tests can import from strategies/options/
_options_engine_path = str(Path(__file__).parent.parent.parent / "strategies" / "options")
if _options_engine_path not in sys.path:
    sys.path.insert(0, _options_engine_path)

from core.strategy_base import StrategyBase
from core.signal import Signal
from core.strategy_context import StrategyContext, PositionView, MarketData

# Inject into builtins so test files that forgot imports still work
builtins.StrategyBase = StrategyBase
builtins.Signal = Signal
builtins.StrategyContext = StrategyContext
builtins.PositionView = PositionView
builtins.MarketData = MarketData
