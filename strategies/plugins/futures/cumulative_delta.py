from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.signal import Signal
from strategies.futures.entry_strategies import strategy_cumulative_delta

class CumulativeDeltaStrategy(StrategyBase):
    """
    Plugin wrapper for Cumulative Delta strategy.
    Corresponds to GSD Wave 5.1 & 5.4.
    """
    
    @property
    def name(self) -> str:
        return "cumulative_delta"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "desc": "Cumulative Delta divergence strategy with real-time trailing protection."
        }

    def init(self, ctx: StrategyContext) -> None:
        """Initialize internal state if needed."""
        pass

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        # Guard: df_5m must exist with sufficient bars
        df = ctx.market.df_5m
        if df is None or df.empty:
            return None
        s_cfg = ctx.config.get("strategy", {}).get("cumulative_delta", {})
        min_bars = max(s_cfg.get("sma_length", 50), s_cfg.get("lookback", 20)) + 2
        if len(df) < min_bars:
            return None

        # Prepare state dict expected by the legacy function
        state = {
            "last_5m": ctx.market.last_bar,
            "df_5m": df,
            "score": ctx.market.last_bar.get("score", 0),
            "stop_loss_pts": ctx.config.get("risk_mgmt", {}).get("stop_loss_pts", 60)
        }
        
        # Execute the logic
        sig_dict = strategy_cumulative_delta(state, ctx.config)
        
        if sig_dict:
            return Signal(
                action=sig_dict["action"],
                reason=sig_dict["reason"],
                stop_loss=sig_dict.get("stop_loss", 60.0),
                break_even_trigger=10.0,
                trail_points=20.0
            )
        return None
