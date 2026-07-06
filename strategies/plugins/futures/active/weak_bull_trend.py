"""
weak_bull_trend — WEAK Regime 防守型多頭趨勢策略.

問題陳述:
- WEAK regime + bias=BULLISH 時，現有策略無法有效進場。
  counter_vwap (反轉)、spring_upthrust (找 spring)、range_mean_reversion (均值回歸)
  都不是順勢多頭策略，導致 WEAK + 強勢多出現交易真空。
- WEAK + 強勢多代表「大環境偏弱但短線方向明確偏多」，
  可能是弱勢反彈、空方回補、區間上緣突破或夜盤低流動性推升。
- 不應該完全沒有策略，但也不能用 TREND regime 的積極多頭策略。

解決方案:
- 防守型順勢做多：價格在 VWAP 之上、EMA 多頭排列、動能溫和向上。
- 小倉位 / 快進快出 / 嚴格停損 / 不加碼。
- 避免追高 (overextended gate)、避免弱勢反彈失敗 (momentum gate)。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext

logger = logging.getLogger(__name__)


class WeakBullTrend(StrategyBase):
    """WEAK regime 防守型多頭趨勢策略 — 快進快出、嚴格停損."""

    @property
    def name(self) -> str:
        return "weak_bull_trend"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "market_regime": "WEAK, CHOP",
            "description": "WEAK regime 防守型多頭：價格>VWAP + EMA 多頭排列 + 溫和動能 (非追高)",
            "indicators": ["bias", "regime", "vwap", "ema_fast", "ema_slow", "mom_velo", "adx", "breakout_strength", "body_size_atr", "bars_since_open"],
            "bias_required": "BULLISH",
            "allowed_regimes": ["WEAK", "CHOP"],
        }

    def init(self, context: StrategyContext) -> None:
        # 優先從 YAML 配置讀取 params 區塊
        params = context.config.get("params", {})
        
        # 止損/止盈 - 防守型盈虧比 ~1.5:1 (快進快出)
        self.stop_atr_mult = params.get("stop_atr_mult", 0.8)   # 緊止損
        self.take_profit_atr_mult = params.get("take_profit_atr_mult", 1.2)  # 保守止盈
        
        # VWAP 距離門檻 (價格必須在 VWAP 之上)
        self.max_vwap_dist_atr = params.get("max_vwap_dist_atr", 2.0)
        
        # 動能門檻 (多頭需要溫和正向動能，但不要過熱)
        self.min_mom_velo_bullish = params.get("min_mom_velo_bullish", 3.0)
        self.max_mom_velo_bullish = params.get("max_mom_velo_bullish", 25.0)
        
        # ADX 上限 (WEAK regime 不應有高 ADX)
        self.max_adx = params.get("max_adx", 30.0)
        
        # 最小成交量確認
        self.min_vol_spike = params.get("min_vol_spike", 1.0)
        
        # 最小突破強度 (避免 weak bounce)
        self.min_breakout_strength = params.get("min_breakout_strength", 0.3)
        
        # 是否要求價格在 VWAP 之上
        self.require_vwap_alignment = params.get("require_vwap_alignment", True)
        
        # Shadow mode (paper trading，關閉)
        self.shadow_mode = params.get("shadow_mode", False)
        
        # 時間止損 (快進快出)
        self.time_stop_minutes = params.get("time_stop_minutes", 12)

        # ── 追高防禦 (Anti-chase Guards) ──
        # 1. 距離 VWAP 的 ATR 倍數上限 (避免偏離太遠追高)
        self.max_distance_from_vwap_atr = params.get("max_distance_from_vwap_atr", 1.2)
        # 2. 前一根 K 棒實體大小上限 (避免追在大陽線之後)
        self.block_if_prior_bar_large_body_atr = params.get("block_if_prior_bar_large_body_atr", 1.5)
        # 3. 開盤後最少 bars 數 (至少 N 根 bar 後才進場，讓 ORB 完成)
        self.min_bars_since_open = params.get("min_bars_since_open", 3)
        
        # 狀態追蹤
        self._entry_ts: datetime | None = None
        self._entry_vwap: float | None = None
        self._entry_price: float | None = None
        self._virtual_pos: dict[str, Any] | None = None

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
            
            # 時間止損 (多頭: 低於進場價且無獲利)
            if elapsed_minutes >= self.time_stop_minutes and unrealized_pnl <= 0:
                logger.info(f"[WEAK_BULL_EXIT] TIME_STOP. elapsed={elapsed_minutes:.1f}m pnl={unrealized_pnl}")
                self._reset_trade_state()
                return Signal("EXIT", "TIME_STOP_WEAK_BULL", confidence=1.0)
            
            self._set_eval(skip_reason="POSITION_OPEN", pnl=unrealized_pnl, elapsed=elapsed_minutes)
            return None

        # B. 虛擬持倉出場 (Shadow Mode)
        if self.shadow_mode and self._virtual_pos:
            v_entry = self._virtual_pos["entry_price"]
            v_ts = self._virtual_pos["entry_ts"]
            v_sl = self._virtual_pos["stop_loss"]
            v_tp = self._virtual_pos["target"]
            
            elapsed = (now - v_ts).total_seconds() / 60
            v_pnl = close - v_entry  # LONG
            
            exit_reason = None
            exit_price = close
            
            if low <= v_sl:
                exit_reason = "SHADOW_STOP_LOSS"
                exit_price = v_sl
            elif high >= v_tp:
                exit_reason = "SHADOW_TAKE_PROFIT"
                exit_price = v_tp
            elif elapsed >= self.time_stop_minutes and v_pnl <= 0:
                exit_reason = "SHADOW_TIME_STOP"
                exit_price = close
            
            if exit_reason:
                final_pnl = exit_price - v_entry
                logger.info(f"[WEAK_BULL_SHADOW_RESULT] exit={exit_reason} entry={v_entry:.0f} exit_p={exit_price:.0f} pnl_pts={final_pnl:.1f} elapsed={elapsed:.1f}m")
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
        breakout_strength = float(bar.get("breakout_strength", 0))
        body_size_atr = float(bar.get("body_size_atr", 0))
        bars_since_open = int(bar.get("bars_since_open", 0))
        regime = str(getattr(context.market, "regime", "UNKNOWN")).upper()
        # [P0] Bias: must read from bar["bias"] (set by router), NOT context.market.bias
        raw_bias = bar.get("router_bias") or bar.get("bias") or "NEUTRAL"
        bias = str(raw_bias).strip().upper()
        price_vs_vwap = float(bar.get("price_vs_vwap", 0))
        
        # 計算距離 VWAP 的 ATR 倍數
        vwap_dist_atr = (close - vwap) / atr if atr > 0 and vwap > 0 else 999
        
        # 簡單 EMA 排列檢查
        ema_bullish = close > ema_fast > ema_slow if ema_fast > 0 and ema_slow > 0 else False

        # ── 3. Regime 門檻 ──
        allowed_regimes = {"WEAK", "CHOP", "STRETCHED"}
        if regime not in allowed_regimes:
            self._set_eval(skip_reason="REGIME_NOT_ALLOWED", regime=regime, allowed=list(allowed_regimes))
            return None

        # ── 4. Bias 門檻 ──
        if bias != "BULLISH":
            self._set_eval(skip_reason="BIAS_NOT_BULLISH", bias=bias)
            return None

        # ── 5. ADX 確認 (WEAK regime 不應有高 ADX) ──
        if adx >= self.max_adx:
            self._set_eval(skip_reason="ADX_TOO_HIGH", adx=adx, max_adx=self.max_adx)
            return None

        # ── 6. VWAP 對齊 (價格必須在 VWAP 之上) ──
        if self.require_vwap_alignment and close <= vwap:
            self._set_eval(skip_reason="BELOW_VWAP", close=close, vwap=vwap)
            return None

        # ── 7. 價格位置 ──
        if vwap_dist_atr > self.max_vwap_dist_atr:
            self._set_eval(skip_reason="TOO_FAR_FROM_VWAP", vwap_dist_atr=vwap_dist_atr, max=self.max_vwap_dist_atr)
            return None

        # ── 7a. 追高防禦：距離 VWAP 太遠 (max_distance_from_vwap_atr) ──
        if (close - vwap) / atr > self.max_distance_from_vwap_atr:
            self._set_eval(skip_reason="TOO_FAR_FROM_VWAP_CHASE", dist_atr=(close - vwap) / atr, max=self.max_distance_from_vwap_atr)
            return None

        # ── 7b. 追高防禦：前一根 K 棒實體過大 (追在大陽線後) ──
        if body_size_atr > self.block_if_prior_bar_large_body_atr:
            self._set_eval(skip_reason="PRIOR_BAR_TOO_LARGE", body_size_atr=body_size_atr, max=self.block_if_prior_bar_large_body_atr)
            return None

        # ── 7c. 追高防禦：開盤初期不進場 (等 ORB 完成) ──
        if bars_since_open < self.min_bars_since_open:
            self._set_eval(skip_reason="TOO_EARLY_AFTER_OPEN", bars_since_open=bars_since_open, min=self.min_bars_since_open)
            return None

        # ── 8. 動能確認 (需要溫和正向，不能過熱或負向) ──
        if mom_velo < self.min_mom_velo_bullish:
            self._set_eval(skip_reason="MOM_VELO_TOO_WEAK", mom_velo=mom_velo, min=self.min_mom_velo_bullish)
            return None
        if mom_velo > self.max_mom_velo_bullish:
            self._set_eval(skip_reason="MOM_VELO_OVERHEATED", mom_velo=mom_velo, max=self.max_mom_velo_bullish)
            return None

        # ── 9. EMA 排列 ──
        if not ema_bullish:
            self._set_eval(skip_reason="EMA_NOT_BULLISH", close=close, ema_fast=ema_fast, ema_slow=ema_slow)
            return None

        # ── 10. 突破強度 ──
        if breakout_strength < self.min_breakout_strength:
            self._set_eval(skip_reason="BREAKOUT_TOO_WEAK", breakout_strength=breakout_strength, min=self.min_breakout_strength)
            return None

        # ── 11. 成交量確認 ──
        # [Night Fix] Relax volume for night session
        is_night = bar.get("is_night_session", False)
        _vol_thresh = 0.8 if is_night else self.min_vol_spike
        if volume_spike < _vol_thresh:
            self._set_eval(skip_reason="VOLUME_TOO_LOW", volume_spike=volume_spike, threshold=_vol_thresh)
            return None

        # ── 12. 信號發射 ──
        self._set_eval(
            triggered=True,
            action="BUY",
            edge_score=0.65,
            regime=regime,
            bias=bias,
            adx=adx,
            mom_velo=mom_velo,
            vwap_dist_atr=vwap_dist_atr,
            ema_bullish=ema_bullish,
            breakout_strength=breakout_strength,
        )
        
        stop_p = close - (atr * self.stop_atr_mult)
        target_p = close + (atr * self.take_profit_atr_mult)
        
        logger.info(
            f"[WEAK_BULL_SIGNAL] close={close:.0f} vwap={vwap:.0f} adx={adx:.1f} "
            f"mom_velo={mom_velo:.1f} ema_bullish={ema_bullish} bs={breakout_strength:.2f}"
        )

        if self.shadow_mode:
            self._virtual_pos = {
                "entry_price": close,
                "entry_ts": now,
                "stop_loss": stop_p,
                "target": target_p
            }
            logger.info(f"[WEAK_BULL_SHADOW_ENTRY] entry={close:.0f} sl={stop_p:.0f} tp={target_p:.0f}")
            return Signal(action="HOLD", reason="SHADOW_WEAK_BULL_TRIGGERED", confidence=0.65)
        
        return Signal(
            action="BUY",
            reason="WEAK_BULL_TREND",
            stop_loss=stop_p,
            target=target_p,
            confidence=0.65,
            quantity=1
        )

    def _reset_trade_state(self):
        self._entry_ts = None
        self._entry_vwap = None
        self._entry_price = None

    def cleanup(self) -> None:
        self._reset_trade_state()
        self._virtual_pos = None
