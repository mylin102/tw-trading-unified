class SpringUpthrustStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "spring_upthrust"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "1.1",
            "backtest_pf": 3.36,
            "backtest_wr": 48,
            "backtest_maxdd": -9.8,
            "market_regime": "ranging",
            "description": "Spring（假跌破）做多 / Upthrust（假突破）做空",
        }

    def init(self, context: StrategyContext) -> None:
        self.lookback = context.config.get("params", {}).get("lookback", 20)
        self.threshold_mult = context.config.get("params", {}).get("threshold_mult", 1.5)

    def on_bar(self, context: StrategyContext) -> Signal | None:
        if context.market.df_5m is None:
            return None

        df = context.market.df_5m
        if len(df) < self.lookback + 5:
            return None

        recent_low = df["low"].iloc[-self.lookback:-1].min()
        recent_high = df["high"].iloc[-self.lookback:-1].max()
        current_low = df["low"].iloc[-1]
        current_high = df["high"].iloc[-1]
        close = df["close"].iloc[-1]
        atr = df["atr"].iloc[-1] if "atr" in df.columns else 0

        signal = None
        reason = ""

        # Spring：價格跌破近期低點後快速拉回（假跌破）
        if current_low < recent_low and close > recent_low + atr * 0.5 and context.position.size <= 0:
            signal = "BUY"
            reason = "SPRING_REVERSAL"
        
        # Upthrust：價格突破近期高點後快速回落（假突破）
        elif current_high > recent_high and close < recent_high - atr * 0.5 and context.position.size >= 0:
            signal = "SELL"
            reason = "UPTHRUST_REVERSAL"

        if signal:
            stop_loss = recent_low - atr if signal == "BUY" else recent_high + atr
            return Signal(
                action=signal,
                reason=reason,
                stop_loss=stop_loss,
                confidence=0.70
            )

        return None
