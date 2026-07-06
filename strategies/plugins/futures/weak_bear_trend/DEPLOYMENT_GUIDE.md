# weak_bear_trend 策略部署指南

## 📊 問題診斷

### 現有 WEAK Regime 策略空白

| 策略名稱 | 類型 | bias=SHORT 時行為 | 問題 |
|---------|------|------------------|------|
| `counter_vwap` | Countertrend | 等 bullish fire 失敗 → **做多** | 不做空 |
| `spring_upthrust` | Countertrend | 等 spring 假跌破 → **做多** | 不做空 |
| `range_mean_reversion` | Mean Reversion | 區間下緣 → **做多** | 不做空 |
| `adaptive_orb` | Breakout | 需要強勢突破 | WEAK regime 不觸發 |
| `trend_continuation_v1` | Trend | 需要 TREND regime | WEAK regime 被阻止 |

**結論**: WEAK + bias=SHORT 時，**沒有趨勢做空策略**

## 🎯 weak_bear_trend 解決方案

### 核心設計理念

```
┌─────────────────────────────────────────────────────────┐
│  WEAK Regime 空頭趨勢策略 (weak_bear_trend)              │
├─────────────────────────────────────────────────────────┤
│  • 不追空：等弱勢反彈失敗後做空                           │
│  • 低門檻：適應 WEAK regime 的震盪特性                    │
│  • 快進快出：嚴格止損 + 時間止損                          │
│  • 偏見依賴：只在 bias=SHORT 時進場                       │
└─────────────────────────────────────────────────────────┘
```

### 進場條件對比

| 條件 | weak_bear_trend | counter_vwap | spring_upthrust |
|------|-----------------|--------------|-----------------|
| **Regime** | WEAK, CHOP | WEAK, CHOP, SQUEEZE | SQUEEZE |
| **Bias** | SHORT (必需) | 無要求 | 無要求 |
| **ADX** | < 22 (弱勢) | 無要求 | 無要求 |
| **進場觸發** | 反彈失敗 + mom_velo<0 | Fire 失敗反轉 | 假突破 BB |
| **本質** | **趨勢延續** | 均值回歸 | 均值回歸 |

## 📈 策略邏輯流程圖

```
                    ┌─────────────────┐
                    │  Bar 進場檢查    │
                    └────────┬────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
       ┌──────▼──────┐              ┌──────▼──────┐
       │ Regime 檢查  │              │ Bias 檢查   │
       │ WEAK/CHOP?  │              │ SHORT?      │
       └──────┬──────┘              └──────┬──────┘
              │                             │
         ✅   │                             │   ❌
    ┌─────────┴────────┐            ┌──────┴──────┐
    │                  │            │  阻止進場   │
    │         ┌────────▼────────┐   └─────────────┘
    │         │   ADX 檢查      │
    │         │   < 22?         │
    │         └────────┬────────┘
    │                  │
    │             ✅   │
    │    ┌─────────────┴─────────────┐
    │    │                           │
    │    │  反彈確認 (過去 5 bars)     │
    │    │  High >= VWAP * 0.9995?  │
    │    └─────────────┬─────────────┘
    │                  │
    │             ✅   │
    │    ┌─────────────┴─────────────┐
    │    │   價格位置檢查            │
    │    │   close < VWAP + 0.8 ATR  │
    │    └─────────────┬─────────────┘
    │                  │
    │             ✅   │
    │    ┌─────────────┴─────────────┐
    │    │   動能確認                │
    │    │   mom_velo < -5?          │
    │    └─────────────┬─────────────┘
    │                  │
    │             ✅   │
    │    ┌─────────────┴─────────────┐
    │    │   成交量確認              │
    │    │   volume_spike >= 1.0?    │
    │    └─────────────┬─────────────┘
    │                  │
    │             ✅   │
    │    ┌─────────────┴─────────────┐
    │    │      SELL 信號            │
    │    │  SL = close + 1.5 ATR     │
    │    │  TP = close - 2.0 ATR     │
    │    │  Confidence = 0.75        │
    │    └───────────────────────────┘
    │
    └─────────────────────────────────
```

## 🔧 部署步驟

### 1. 策略文件已就位

```bash
# 策略主文件
~/Documents/mylin102/tw-trading-unified/strategies/plugins/futures/weak_bear_trend.py

# 配置文件
~/Documents/mylin102/tw-trading-unified/config/strategies/weak_bear_trend.yaml

# 測試文件
~/Documents/mylin102/tw-trading-unified/tests/test_weak_bear_trend_simple.py

# 文檔
~/Documents/mylin102/tw-trading-unified/strategies/plugins/futures/weak_bear_trend/README.md
```

### 2. 策略註冊 (已完成 ✅)

已更新 `core/futures_strategy_router.py`:

```python
STRATEGY_POLICY: dict[str, dict] = {
    # ...
    "weak_bear_trend": {
        "enabled_regimes": ["WEAK", "CHOP"],
        "max_weight": 0.5,
        "kill_if_cagr_below": -0.05,
        "required_bias": "SHORT",
        "description": "WEAK regime 空头趋势：弱勢反彈失敗後做空",
    },
    # ...
}

# 策略列表更新
weak_strategies = (..., "weak_bear_trend")
bear_strategies = (..., "weak_bear_trend")
```

### 3. 配置參數調整

編輯 `config/strategies/weak_bear_trend.yaml`:

```yaml
params:
  # 風控參數 (可根據回測調整)
  stop_atr_mult: 1.5          # 止損緊一些 (WEAK 反轉快)
  take_profit_atr_mult: 2.0   # 盈虧比 1:1.33
  max_vwap_dist_atr: 0.8      # 不追空
  
  # 進場門檻
  min_mom_velo_bearish: -5.0  # 動能向下加速
  max_adx: 22.0               # WEAK regime 特徵
  min_vol_spike: 1.0          # 不需要放量
  
  # 確認參數
  lookback_bars: 5            # 反彈確認 K 棒數
  time_stop_minutes: 20       # 時間止損
  
  # 運行模式
  shadow_mode: true           # 先用虛擬單驗證
```

### 4. 測試驗證

```bash
cd ~/Documents/mylin102/tw-trading-unified
python3 tests/test_weak_bear_trend_simple.py
```

預期輸出:
```
✅ PASS: 標準空頭進場
✅ PASS: TREND regime 阻止
✅ PASS: LONG bias 阻止
✅ PASS: ADX 過高阻止
✅ PASS: 動能不夠向下
✅ PASS: Shadow mode
總計：6/6 通過
```

### 5. 回測建議

```bash
# 回測 (shadow mode)
python backtest/main.py \
  --strategy weak_bear_trend \
  --regime WEAK \
  --bias SHORT \
  --start-date 2026-01-01 \
  --end-date 2026-05-07

# 參數優化
python scripts/optimize/optimize_weak_bear.py \
  --param stop_atr_mult --range 1.0 2.0 --step 0.1 \
  --param take_profit_atr_mult --range 1.5 3.0 --step 0.1
```

### 6. 實盤啟用 (Shadow Mode → Live)

**第一階段：Shadow Mode (1-2 週)**
```yaml
shadow_mode: true  # 記錄虛擬單，不下真實訂單
```

觀察指標:
- 進場頻率 (預期：WEAK+SHORT 時 1-3 次/天)
- 虛擬盈虧比 (目標：> 1.2)
- 勝率 (預期：45-55%，因為是趨勢策略)
- 最大回撤 (警戒線：-5%)

**第二階段：Live Trading (確認後)**
```yaml
shadow_mode: false  # 啟用真實訂單
```

## 📊 監控與風控

### 日常監控檢查清單

- [ ] **Regime 匹配**: 確認只在 WEAK/CHOP 進場
- [ ] **Bias 正確**: 確認 bias=SHORT (檢查 `core/futures_bar_regime.py`)
- [ ] **止損執行**: 確認 1.5 ATR 止損嚴格執行
- [ ] **時間止損**: 20 分鐘無獲利是否出場
- [ ] **Shadow PnL**: 虛擬單績效追蹤

### 風險警示

| 風險 | 影響 | 緩解措施 |
|------|------|----------|
| **Bias 錯誤** | 連續虧損 | 監控 bias 準確率，< 50% 時停用 |
| **WEAK 轉 TREND** | 策略不適應 | Router 會自動阻止 (regime gate) |
| **快速反轉** | 止損被觸發 | 時間止損 + 嚴格 1.5 ATR 止損 |
| **過度交易** | 手續費侵蝕利潤 | max_weight=0.5, 限制倉位 |

### 停損規則 (Kill Switch)

當以下任一條件觸發時，自動停用策略：

```python
if strategy_cagr < -0.05:  # 年化報酬率 < -5%
    disable_strategy("weak_bear_trend")
    
if strategy_maxdd < -0.10:  # 最大回撤 < -10%
    disable_strategy("weak_bear_trend")
    
if win_rate < 0.35 and trades > 20:  # 勝率 < 35% 且交易 > 20 次
    disable_strategy("weak_bear_trend")
```

## 📈 預期績效

基於 WEAK regime 特性和策略設計：

| 指標 | 預期值 | 說明 |
|------|--------|------|
| **勝率** | 45-55% | 趨勢策略，不追求高勝率 |
| **盈虧比** | 1.3-1.5 | 止損 1.5 ATR, 止盈 2.0 ATR |
| **Profit Factor** | 1.3-1.6 | 中等水準 |
| **MaxDD** | -5% ~ -8% | 嚴格止損控制 |
| **Sharpe** | 0.8-1.2 | 中等風險調整後報酬 |
| **進場頻率** | 1-3 次/天 | WEAK+SHORT 條件下 |

## 🔄 與現有策略的協同效應

### WEAK + bias=SHORT 情境

```
Router 決策流程:

1. counter_vwap: 等 bullish fire 失敗 → 做多 (countertrend)
   └─ 信心：0.80-0.85
   └─ 與 weak_bear_trend 方向相反

2. spring_upthrust: 等 spring 假跌破 → 做多 (countertrend)
   └─ 信心：0.70
   └─ 與 weak_bear_trend 方向相反

3. weak_bear_trend: 等反彈失敗 → 做空 (trend) ← 新增
   └─ 信心：0.75
   └─ 唯一做空選項

Router 選擇:
- 如果 counter_vwap 觸發 → 做多 (信心較高)
- 如果 weak_bear_trend 觸發 → 做空 (唯一趨勢做空)
- 如果同時觸發 → 根據優先級和信心分數選擇
```

### 策略組合建議

**保守組合** (偏重 countertrend):
```yaml
counter_vweight: 0.8
spring_upthrust_weight: 0.5
weak_bear_trend_weight: 0.3  # 較低權重
```

**平衡組合**:
```yaml
counter_vwap_weight: 0.5
spring_upthrust_weight: 0.5
weak_bear_trend_weight: 0.5
```

**積極組合** (增加趨勢曝險):
```yaml
counter_vwap_weight: 0.3
spring_upthrust_weight: 0.3
weak_bear_trend_weight: 0.8  # 較高權重
```

## 📝 版本歷史

- **v1.0** (2026-05-07): 初始版本
  - 填補 WEAK regime 空头趨勢空白
  - 反彈失敗進場邏輯
  - 嚴格止損 + 時間止損
  - Shadow mode 支援

## 🎓 使用教學

### 情境 1: 夜盤空頭弱勢

```
時間：22:30
Regime: WEAK
Bias: SHORT
價格：22000
VWAP: 22050

情境:
- 22:00-22:25: 價格反彈至 22040 (接近 VWAP)
- 22:30: 價格下跌至 22000, mom_velo=-8, ADX=18

weak_bear_trend 行為:
✅ 進場：SELL @ 22000
   止損：22075 (22000 + 1.5*50)
   止盈：21900 (22000 - 2.0*50)
   信心：0.75
```

### 情境 2: 日盤震盪偏空

```
時間：10:15
Regime: CHOP
Bias: SHORT
價格：22100
VWAP: 22120

情境:
- 過去 5 bars 最高價 22160 (接近 ema_fast)
- 當前：close=22100 < ema_fast=22130 < ema_slow=22180
- mom_velo=-12, ADX=20

weak_bear_trend 行為:
✅ 進場：SELL @ 22100
   止損：22175
   止盈：22000
```

## ❓ FAQ

**Q: 為什麼不直接修改 counter_vwap 讓它做空？**

A: counter_vwap 的本質是 countertrend (反向交易)，強迫它做空會破壞策略邏輯。weak_bear_trend 是趨勢延續策略，兩者是不同哲學。

**Q: Shadow mode 要跑多久才能轉 Live？**

A: 建議至少 1-2 週，或累積 20+ 筆虛擬交易。觀察指標：
- 勝率 > 40%
- 盈虧比 > 1.2
- MaxDD < -5%

**Q: 如果 bias 判斷錯誤怎麼辦？**

A: 這是最大風險。建議：
1. 監控 bias 準確率 (應 > 60%)
2. 如果 bias 連續錯誤，暫停策略
3. 考慮加入額外的 bias 確認條件

**Q: 可以將這個策略用於 TREND regime 嗎？**

A: 不建議。TREND regime 應該用 `trend_continuation_v1` 或 `adaptive_orb`。weak_bear_trend 專門針對 WEAK 的低 ADX 環境設計。

---

## ✅ 部署檢查清單

- [x] 策略文件創建 (`weak_bear_trend.py`)
- [x] 配置文件創建 (`weak_bear_trend.yaml`)
- [x] 策略註冊 (`futures_strategy_router.py`)
- [x] 单元测试 (`test_weak_bear_trend_simple.py`)
- [x] 文檔創建 (`README.md`, `DEPLOYMENT_GUIDE.md`)
- [ ] 回測驗證 (待執行)
- [ ] Shadow Mode 運行 (待執行)
- [ ] Live Trading 啟用 (待執行)

---

**部署完成後，請回報：**
1. Shadow mode 運行結果
2. 回測績效指標
3. 任何觀察到的異常行為
