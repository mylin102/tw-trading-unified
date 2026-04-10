import builtins
from core.strategy_base import StrategyBase
from core.signal import Signal
from core.strategy_context import StrategyContext, PositionView, MarketData

# Inject into builtins so test files that forgot imports still work
builtins.StrategyBase = StrategyBase
builtins.Signal = Signal
builtins.StrategyContext = StrategyContext
builtins.PositionView = PositionView
builtins.MarketData = MarketData
