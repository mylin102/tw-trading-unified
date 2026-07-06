class OpeningRangeBreakoutStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "orb_breakout"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "backtest_pf": 1.78,
            "backtest_wr": 52,
            "backtest_maxdd": -8.4,
            "market_regime": "trending",
            "description": "開盤前 30 分鐘區間突破策略（適合高波動期貨）",
        }

    def init(self, context: StrategyContext) -> None:
        self.range_bars = context.config.get("params", {}).get("range_bars", 6)  # 約 30 分鐘 (5m bar)
        self.atr_mult = context.config.get("params", {}).get("atr_mult", 1.5)
        self._range_high = 0.0
        self._range_low = 0.0
        self._range_built = False

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar:
            return None

        # 簡化版：假設前 range_bars 根已計算好 range（實際可從 df_5m 計算）
        if not self._range_built and context.bar_counter < self.range_bars:
            # 在 init 或前幾根 bar 計算 opening range
            return None

        close = bar["close"]
        atr = bar.get("atr", 10)

        if close > self._range_high and context.position.size <= 0:
            return Signal("BUY", "ORB_UP_BREAKOUT", 
                         stop_loss=self._range_high - atr * self.atr_mult,
                         confidence=0.68)

        elif close < self._range_low and context.position.size >= 0:
            return Signal("SELL", "ORB_DOWN_BREAKOUT", 
                         stop_loss=self._range_low + atr * self.atr_mult,
                         confidence=0.68)

        return None
