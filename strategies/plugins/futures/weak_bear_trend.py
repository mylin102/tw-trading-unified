"""
weak_bear_trend — WEAK Regime 专用空头趋势策略.

问题陈述:
- WEAK regime 下，现有策略 (counter_vwap, spring_upthrust, range_mean_reversion) 都是 countertrend/mean reversion 型
- 空頭市場 (bias=SHORT) 時，這些策略不會做空 (counter_vwap 做反轉、spring_upthrust 找 spring)
- 導致 WEAK regime + bias=SHORT 出現交易真空。

解決方案:
- 當趨勢處於 WEAK regime 但方向明確為 SHORT 時，執行「弱勢空頭趨勢」策略。
- 進場條件：價格接近 VWAP 但反彈無力 (mom_velo 向下)，且 ADX 尚未飆高 (尚未進入強趨勢)。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext

logger = logging.getLogger(__name__)


class WeakBearTrend(StrategyBase):
    """WEAK regime 专用空头趨勢策略."""

    @property
    def name(self) -> str:
        return "weak_bear_trend"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.1",
            "market_regime": "WEAK, CHOP",
            "description": "WEAK regime 空头趋势：弱勢反彈失敗後做空 (非 countertrend)",
            "indicators": ["bias", "regime", "vwap", "ema_fast", "ema_slow", "mom_velo", "adx"],
            "bias_required": "SHORT",
            "allowed_regimes": ["WEAK", "CHOP"],
        }

    def init(self, context: StrategyContext) -> None:
        # 優先從 YAML 配置讀取 params 區塊
        params = context.config.get("params", {})
        
        # 止損/止盈 - 盈虧比 2:1 (大賺小賠)
        self.stop_atr_mult = params.get("stop_atr_mult", 1.0)  # 緊止損 (小賠)
        self.take_profit_atr_mult = params.get("take_profit_atr_mult", 2.0)  # 合理止盈 (大賺)
        
        # VWAP 距離門檻
        self.max_vwap_dist_atr = params.get("max_vwap_dist_atr", 0.5)
        
        # 動能門檻 (更嚴格，確保進場品質)
        self.min_mom_velo_bearish = params.get("min_mom_velo_bearish", -8.0)
        
        # ADX 上限 (放寬以覆蓋強夜盤趨勢)
        self.max_adx = params.get("max_adx", 50.0)
        
        # 最小成交量確認
        self.min_vol_spike = params.get("min_vol_spike", 1.0)
        
        # 反彈確認 bars
        self.lookback_bars = params.get("lookback_bars", 5)
        
        # Shadow mode
        self.shadow_mode = params.get("shadow_mode", True)
        
        # 時間止損
        self.time_stop_minutes = params.get("time_stop_minutes", 15)
        
        # 狀態追蹤
        self._entry_ts: datetime | None = None
        self._entry_vwap: float | None = None
        self._entry_price: float | None = None
        self._virtual_pos: dict[str, Any] | None = None
        self._recent_high_near_vwap = False

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None
        
        # Ensure base variables exist (defensive)
        if not hasattr(self, 'max_adx'):
            self.init(context)

        # ═══ Warm-up Guard ═══
        mom_state = int(bar.get("mom_state", 0))
        if mom_state == 999:
            self._set_eval(skip_reason="INDICATORS_WARMING_UP", mom_state=999)
            return None

        ts = bar.get("timestamp")
        if isinstance(ts, datetime):
            now = ts
        elif isinstance(ts, str):
            now = datetime.fromisoformat(ts)
        else:
            now = datetime.now()

        close = float(bar.get("Close", 0))
        high = float(bar.get("High", 0))
        low = float(bar.get("Low", 0))
        atr = float(bar.get("atr", 50))
        
        # ── 1. 持倉管理 (平倉邏輯) ──
        pos_size = context.position.size
        
        # A. 真實持倉出場
        if pos_size != 0:
            if self._entry_ts is None:
                self._entry_ts = now
                self._entry_vwap = float(bar.get("vwap", 0))
                self._entry_price = context.position.entry_price

            elapsed_minutes = (now - self._entry_ts).total_seconds() / 60
            unrealized_pnl = context.position.unrealized_pnl
            
            # 時間止損
            if elapsed_minutes >= self.time_stop_minutes and unrealized_pnl <= 0:
                logger.info(f"[WEAK_BEAR_EXIT] TIME_STOP. elapsed={elapsed_minutes:.1f}m pnl={unrealized_pnl}")
                self._reset_trade_state()
                return Signal("EXIT", "TIME_STOP_WEAK_BEAR", confidence=1.0)
            
            self._set_eval(skip_reason="POSITION_OPEN", pnl=unrealized_pnl, elapsed=elapsed_minutes)
            return None

        # B. 虛擬持倉出場 (Shadow Mode)
        if self.shadow_mode and self._virtual_pos:
            v_entry = self._virtual_pos["entry_price"]
            v_ts = self._virtual_pos["entry_ts"]
            v_sl = self._virtual_pos["stop_loss"]
            v_tp = self._virtual_pos["target"]
            
            elapsed = (now - v_ts).total_seconds() / 60
            v_pnl = v_entry - close  # SHORT
            
            exit_reason = None
            exit_price = close
            
            if high >= v_sl:
                exit_reason = "SHADOW_STOP_LOSS"
                exit_price = v_sl
            elif low <= v_tp:
                exit_reason = "SHADOW_TAKE_PROFIT"
                exit_price = v_tp
            elif elapsed >= self.time_stop_minutes and v_pnl <= 0:
                exit_reason = "SHADOW_TIME_STOP"
                exit_price = close
            
            if exit_reason:
                final_pnl = v_entry - exit_price
                logger.info(f"[WEAK_BEAR_SHADOW_RESULT] exit={exit_reason} entry={v_entry:.0f} exit_p={exit_price:.0f} pnl_pts={final_pnl:.1f} elapsed={elapsed:.1f}m")
                self._virtual_pos = None
            else:
                self._set_eval(skip_reason="VIRTUAL_POSITION_OPEN", pnl=v_pnl)
                return None

        # 重置狀態
        if pos_size == 0:
            self._reset_trade_state()

        # ── 2. 數據提取 ──
        vwap = float(bar.get("vwap", 0))
        ema_fast = float(bar.get("ema_fast", 0))
        ema_slow = float(bar.get("ema_slow", 0))
        mom_velo = float(bar.get("mom_velo", 0))
        adx = float(bar.get("adx", 0))
        volume_spike = float(bar.get("volume_spike", 1.0))
        regime = str(getattr(context.market, "regime", "UNKNOWN")).upper()
        # [P0] Bias: must read from bar["bias"] (set by router), NOT context.market.bias
        raw_bias = bar.get("router_bias") or bar.get("bias") or "NEUTRAL"
        bias = str(raw_bias).strip().upper()
        import logging as _wb2
        _wb2.getLogger(__name__).info(
            "[WEAK_BEAR_BIAS_CONTRACT] raw_bias=%r normalized_bias=%s bar_keys_sample=%s",
            raw_bias, bias, sorted(bar.keys())[:15],
        )
        price_vs_vwap = float(bar.get("price_vs_vwap", 0))
        
        # 計算距離 VWAP 的 ATR 倍數
        vwap_dist_atr = (close - vwap) / atr if atr > 0 and vwap > 0 else 999
        
        # 檢查反彈歷史
        self._recent_high_near_vwap = True
        df = context.market.df_5m
        if df is not None and self.lookback_bars > 0:
            try:
                if hasattr(df, 'iloc') and len(df) >= self.lookback_bars:
                    recent_highs = df["High"].iloc[-self.lookback_bars:].max()
                    if recent_highs >= vwap * 0.9995:
                        self._recent_high_near_vwap = True
                    else:
                        self._recent_high_near_vwap = False
            except:
                pass

        # ── 3. Regime 門檻 ──
        allowed_regimes = {"WEAK", "CHOP", "STRETCHED"}
        if regime not in allowed_regimes:
            self._set_eval(skip_reason="REGIME_NOT_ALLOWED", regime=regime, allowed=list(allowed_regimes))
            return None

        # ── 4. Bias 門檻 ──
        import logging as _wb
        _wb.getLogger(__name__).info(
            "[WEAK_BEAR_BIAS_TRACE] raw_bias=%r normalized_bias=%s close=%s vwap=%s",
            bar.get("bias"),
            bias,
            bar.get("Close"),
            bar.get("vwap"),
        )
        if bias != "SHORT":
            self._set_eval(skip_reason="BIAS_NOT_SHORT", bias=bias)
            return None

        # ── 5. ADX 確認 ──
        if adx >= self.max_adx:
            self._set_eval(skip_reason="ADX_TOO_HIGH", adx=adx, max_adx=self.max_adx)
            return None

        # ── 6. 反彈確認 (暫時關閉 — 觀察 direct short) ──
        # if not self._recent_high_near_vwap:
        #     self._set_eval(skip_reason="NO_RECENT_REBOUND", recent_high_near_vwap=self._recent_high_near_vwap)
        #     return None
        # ── 7. 價格位置 ──
        if vwap_dist_atr > self.max_vwap_dist_atr:
            self._set_eval(skip_reason="TOO_FAR_FROM_VWAP", vwap_dist_atr=vwap_dist_atr, max=self.max_vwap_dist_atr)
            return None

        # ── 8. 動能確認 ──
        if mom_velo >= self.min_mom_velo_bearish:
            self._set_eval(skip_reason="MOM_VELO_NOT_BEARISH_ENOUGH", mom_velo=mom_velo, min=self.min_mom_velo_bearish)
            return None

        # ── 9. EMA 排列 ──
        ema_bearish = close < ema_fast < ema_slow if ema_fast > 0 and ema_slow > 0 else False
        
        # ── 10. 成交量確認 ──
        if volume_spike < self.min_vol_spike:
            self._set_eval(skip_reason="VOLUME_TOO_LOW", volume_spike=volume_spike)
            return None

        # ── 11. 信號發射 ──
        self._set_eval(
            triggered=True,
            action="SELL",
            edge_score=0.75,
            regime=regime,
            bias=bias,
            adx=adx,
            mom_velo=mom_velo,
            vwap_dist_atr=vwap_dist_atr,
            ema_bearish=ema_bearish,
        )
        
        stop_p = close + (atr * self.stop_atr_mult)
        target_p = close - (atr * self.take_profit_atr_mult)
        
        logger.info(
            f"[WEAK_BEAR_SIGNAL] close={close:.0f} vwap={vwap:.0f} adx={adx:.1f} "
            f"mom_velo={mom_velo:.1f} dist_atr={vwap_dist_atr:.2f} ema_bearish={ema_bearish}"
        )

        if self.shadow_mode:
            self._virtual_pos = {
                "entry_price": close,
                "entry_ts": now,
                "stop_loss": stop_p,
                "target": target_p
            }
            logger.info(f"[WEAK_BEAR_SHADOW_ENTRY] entry={close:.0f} sl={stop_p:.0f} tp={target_p:.0f}")
            return Signal(action="HOLD", reason="SHADOW_WEAK_BEAR_TRIGGERED", confidence=0.75)
        
        return Signal(
            action="SELL",
            reason="WEAK_BEAR_TREND",
            stop_loss=stop_p,
            target=target_p,
            confidence=0.75,
            quantity=1
        )

    def _reset_trade_state(self):
        self._entry_ts = None
        self._entry_vwap = None
        self._entry_price = None
        self._recent_high_near_vwap = False

    def cleanup(self) -> None:
        self._reset_trade_state()
        self._virtual_pos = None
