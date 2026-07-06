class SqueezeMomentumStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "squeeze_momentum"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "backtest_pf": 2.45,
            "backtest_wr": 42,
            "backtest_maxdd": -11.5,
            "market_regime": "squeeze",
            "description": "Bollinger Band + Keltner Channel 擠壓後動能爆發",
        }

    def init(self, context: StrategyContext) -> None:
        self.min_squeeze_bars = context.config.get("params", {}).get("min_squeeze_bars", 3)

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar or "squeeze_on" not in bar or "momentum" not in bar:
            return None

        if not bar["squeeze_on"]:   # 不在擠壓狀態
            return None

        momentum = bar["momentum"]   # 假設指標已計算（可使用 TTMsqueeze 或自訂）

        if momentum > 0 and context.position.size <= 0:
            return Signal("BUY", "SQUEEZE_MOMENTUM_UP", 
                         stop_loss=bar["close"] - bar.get("atr", 10)*1.8, 
                         confidence=0.65)
        
        elif momentum < 0 and context.position.size >= 0:
            return Signal("SELL", "SQUEEZE_MOMENTUM_DOWN", 
                         stop_loss=bar["close"] + bar.get("atr", 10)*1.8, 
                         confidence=0.65)

        return None
