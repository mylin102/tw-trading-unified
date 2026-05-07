# weak_bear_trend — WEAK Regime 空头趋势策略

## 問題陳述

在 WEAK regime 下，現有策略都是 countertrend/mean reversion 型：
- `counter_vwap`: Squeeze Fire 失敗後**反向**進場 (多→空，空→多)
- `spring_upthrust`: 假突破**反向**進場 (Spring 做多，Upthrust 做空)
- `range_mean_reversion`: 區間**回歸**交易

**問題**: 當 bias=SHORT (空頭市場) 時：
- counter_vwap 不做空 (等 bullish fire 失敗才做多)
- spring_upthrust 不做空 (等 spring 假跌破做多)
- range_mean_reversion 不做空 (在區間下緣做多)

**結果**: WEAK regime + 空頭市場 = 沒有趨勢做空策略

## 解決方案

`weak_bear_trend` 專門填補這個空白：

| 維度 | weak_bear_trend | counter_vwap | spring_upthrust |
|------|----------------|--------------|-----------------|
| **交易方向** | SHORT ONLY | 雙向 (countertrend) | 雙向 (countertrend) |
| **進場邏輯** | 弱勢反彈失敗 | Fire 失敗反轉 | 假突破反轉 |
| **Regime** | WEAK, CHOP | WEAK, CHOP, SQUEEZE | SQUEEZE |
| **Bias 要求** | SHORT | 無 | 無 |
| **本質** | **趨勢延續** | 均值回歸 | 均值回歸 |

## 核心邏輯

```
IF regime in {WEAK, CHOP} 
AND bias == SHORT
AND ADX < 22 (弱勢市場)
AND 曾有反彈接近 VWAP (過去 5 bars)
AND 價格在 VWAP 之下或附近 (< 0.8 ATR)
AND mom_velo < -5 (動能向下加速)
AND volume_spike >= 1.0
THEN SELL (做空)
```

## 技術指標

- **VWAP**: 價格必須接近或低於 VWAP (不追空)
- **EMA 排列**: `close < ema_fast < ema_slow` (加分項)
- **mom_velo**: 動能變化率，要求 < -5 (向下加速)
- **ADX**: < 22 (確保是 WEAK regime，不是 TREND)
- **volume_spike**: >= 1.0 (不需要放量)

## 風控參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `stop_atr_mult` | 1.5 | 止損 (比 TREND 策略緊) |
| `take_profit_atr_mult` | 2.0 | 止盈 |
| `max_vwap_dist_atr` | 0.8 | 最大 VWAP 距離 (ATR 倍數) |
| `max_adx` | 22.0 | ADX 上限 |
| `min_mom_velo_bearish` | -5.0 | 最小動能向下加速 |
| `lookback_bars` | 5 | 反彈確認 K 棒數 |
| `time_stop_minutes` | 20 | 時間止損 (WEAK 反轉快) |

## 進場範例

### 情境 1: VWAP 拒絕
```
夜盤 22:30, bias=SHORT, regime=WEAK
- 22:00-22:25: 價格反彈至 VWAP 附近 (22050 → 22100, VWAP=22110)
- 22:30: 價格下跌至 22080, mom_velo=-8, ADX=18
→ SELL @ 22080, SL=22170, TP=21980
```

### 情境 2: EMA 壓制
```
日盤 10:15, bias=SHORT, regime=CHOP
- 過去 5 bars 最高價 22150 (接近 ema_fast=22160)
- 當前：close=22100 < ema_fast=22160 < ema_slow=22200
- mom_velo=-12, volume_spike=1.1
→ SELL @ 22100, SL=22180, TP=22000
```

## 回測建議

```bash
# 回測 ( shadow mode 先跑虛擬單)
python backtest/main.py --strategy weak_bear_trend --regime WEAK --bias SHORT

# 參數優化
python scripts/optimize/weak_bear_trend_optimize.py
```

## 註冊到系統

在 `core/futures_strategy_router.py` 中添加：

```python
"weak_bear_trend": {
    "func": strategy_weak_bear_trend,
    "allowed_regimes": ["WEAK", "CHOP"],
    "required_bias": "SHORT",
    "priority": 3,  # 低於 counter_vwap/spring_upthrust
},
```

## 風險提示

1. **WEAK regime 反轉快**: 時間止損設為 20 分鐘，不戀戰
2. **不追空**: 必須等待反彈失敗，避免高點接刀
3. **Shadow mode 優先**: 先用虛擬單驗證，確認邏輯正確再開實盤
4. **偏見依賴**: 完全依賴 bias=SHORT，bias 錯誤會導致連續虧損

## 與現有策略的協同

```
WEAK + bias=SHORT:
├─ counter_vwap: 等 bullish fire 失敗 → 做多 (countertrend)
├─ spring_upthrust: 等 spring 假跌破 → 做多 (countertrend)
└─ weak_bear_trend: 等反彈失敗 → 做空 (trend) ← 新增

結果: WEAK + 空頭市場下，同時有趨勢做空 + 均值回歸做多選項
     由 Router 根據優先級和信心分數選擇
```

## 版本歷史

- **v1.0** (2026-05-07): 初始版本，填補 WEAK regime 空头趨勢空白
