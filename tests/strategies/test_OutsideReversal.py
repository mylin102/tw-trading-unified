class OutsideBarReversalStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "outside_bar_reversal"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "backtest_pf": 1.68,
            "backtest_wr": 58,
            "backtest_maxdd": -10.1,
            "market_regime": "ranging",
            "description": "外包 K 線後的反轉或假突破捕捉",
            "backtest_expectancy": 0.58,
        }

    def on_bar(self, context: StrategyContext) -> Signal | None:
        if context.market.df_5m is None or len(context.market.df_5m) < 3:
            return None

        df = context.market.df_5m
        prev_high = df["high"].iloc[-2]
        prev_low = df["low"].iloc[-2]
        curr_high = df["high"].iloc[-1]
        curr_low = df["low"].iloc[-1]
        close = df["close"].iloc[-1]
        atr = df["atr"].iloc[-1]

        if curr_high > prev_high and curr_low < prev_low:  # 外包線
            if close > (prev_high + prev_low)/2 and context.position.size <= 0:
                return Signal("BUY", "OUTSIDE_BAR_BULLISH", 
                             stop_loss=prev_low - atr*0.5, confidence=0.65)
            elif close < (prev_high + prev_low)/2 and context.position.size >= 0:
                return Signal("SELL", "OUTSIDE_BAR_BEARISH", 
                             stop_loss=prev_high + atr*0.5, confidence=0.65)

        return None
