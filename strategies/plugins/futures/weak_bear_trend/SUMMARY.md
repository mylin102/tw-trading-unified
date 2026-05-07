# weak_bear_trend 策略摘要

## 🎯 核心問題

**WEAK regime + bias=SHORT 時，沒有趨勢做空策略**

現有策略都是 countertrend/mean reversion：
- `counter_vwap`: 等 bullish fire 失敗 → **做多**
- `spring_upthrust`: 等 spring 假跌破 → **做多**  
- `range_mean_reversion`: 區間下緣 → **做多**

## 💡 解決方案

**weak_bear_trend** — WEAK regime 专用空头趋势策略

| 特徵 | 說明 |
|------|------|
| **類型** | 趨勢延續 (Trend Continuation) |
| **方向** | SHORT ONLY |
| **Regime** | WEAK, CHOP |
| **Bias** | SHORT (必需) |
| **進場** | 弱勢反彈失敗後做空 |
| **止損** | 1.5 ATR (嚴格) |
| **止盈** | 2.0 ATR |
| **時間止損** | 20 分鐘 |

## 📊 進場條件

```
✅ regime in {WEAK, CHOP}
✅ bias == SHORT
✅ ADX < 22 (弱勢市場)
✅ 過去 5 bars 曾有反彈接近 VWAP
✅ 價格在 VWAP 之下或附近 (< 0.8 ATR)
✅ mom_velo < -5 (動能向下加速)
✅ volume_spike >= 1.0
→ SELL
```

## 🔧 關鍵參數

```yaml
stop_atr_mult: 1.5          # 止損
take_profit_atr_mult: 2.0   # 止盈
max_vwap_dist_atr: 0.8      # 不追空
min_mom_velo_bearish: -5.0  # 動能門檻
max_adx: 22.0               # WEAK 特徵
lookback_bars: 5            # 反彈確認
time_stop_minutes: 20       # 時間止損
shadow_mode: true           # 先用虛擬單
```

## 📈 預期績效

| 指標 | 預期值 |
|------|--------|
| 勝率 | 45-55% |
| 盈虧比 | 1.3-1.5 |
| Profit Factor | 1.3-1.6 |
| MaxDD | -5% ~ -8% |
| 進場頻率 | 1-3 次/天 (WEAK+SHORT) |

## 🚀 部署狀態

- [x] 策略文件：`strategies/plugins/futures/weak_bear_trend.py`
- [x] 配置文件：`config/strategies/weak_bear_trend.yaml`
- [x] 策略註冊：`core/futures_strategy_router.py`
- [x] 单元测试：`tests/test_weak_bear_trend_simple.py` ✅ 6/6 通過
- [x] 文檔：`README.md`, `DEPLOYMENT_GUIDE.md`
- [ ] 回測：待執行
- [ ] Shadow Mode: 待運行
- [ ] Live: 待啟用

## 📝 下一步

1. **執行回測**: 驗證歷史績效
   ```bash
   python backtest/main.py --strategy weak_bear_trend --regime WEAK --bias SHORT
   ```

2. **Shadow Mode 運行**: 1-2 週虛擬單驗證

3. **參數優化**: 根據回測調整止損/止盈/門檻

4. **Live 啟用**: 確認績效後轉真實交易

## ⚠️ 風險提示

- **Bias 依賴**: 完全依賴 bias=SHORT，bias 錯誤會導致虧損
- **快速反轉**: WEAK regime 反轉快，嚴格執行時間止損
- **不追空**: 必須等待反彈失敗，避免高點接刀

## 📚 完整文檔

- 策略說明：`strategies/plugins/futures/weak_bear_trend/README.md`
- 部署指南：`strategies/plugins/futures/weak_bear_trend/DEPLOYMENT_GUIDE.md`
- 策略代碼：`strategies/plugins/futures/weak_bear_trend.py`
