class SessionBiasMomentumStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "session_bias_momentum"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "backtest_pf": 1.92,
            "backtest_wr": 51,
            "backtest_maxdd": -9.2,
            "market_regime": "all",
            "description": "結合 session 偏好與短期動能的趨勢延續策略",
            "backtest_expectancy": 0.65,
        }

    def init(self, context: StrategyContext) -> None:
        self.momentum_period = context.config.get("params", {}).get("momentum_period", 10)

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar or "momentum" not in bar:
            return None

        momentum = bar["momentum"]
        session = context.market.session  # 1=日盤, 2=夜盤

        # 日盤偏多 + 正動能 → 做多
        if session == 1 and momentum > 0 and context.position.size <= 0:
            return Signal("BUY", "DAY_SESSION_MOMENTUM", 
                         stop_loss=bar["close"] - bar.get("atr", 10)*1.8, confidence=0.62)

        # 夜盤偏空 + 負動能 → 做空
        elif session == 2 and momentum < 0 and context.position.size >= 0:
            return Signal("SELL", "NIGHT_SESSION_MOMENTUM", 
                         stop_loss=bar["close"] + bar.get("atr", 10)*1.8, confidence=0.62)

        return None
