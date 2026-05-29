# Dashboard weak_bear_trend 監控面板設置完成

## ✅ 已完成的更新

### 1. 創建監控組件

**文件**: `ui/weak_bear_monitor.py`

**功能**:
- ✅ auto_select 配置狀態顯示
- ✅ 微台指 (TMF) 設定顯示
- ✅ weak_bear_trend 參數顯示
- ✅ Regime → 策略映射表
- ✅ 即時 Router Trace 解析
- ✅ 預期行為說明
- ✅ 監控清單

### 2. Dashboard 整合

**文件**: `ui/dashboard.py`

**更新**:
- ✅ 添加 `render_weak_bear_panel()` 函數
- ✅ Sidebar 添加快速入口按鈕
- ✅ 總覽頁面添加監控面板渲染

### 3. PM2 重啟

```bash
pm2 restart dashboard --update-env
```

**狀態**: ✅ Dashboard 運行中 (PID 41372)

---

## 🎯 Dashboard 新功能

### Sidebar 快速入口

在 Dashboard 左側邊欄，你會看到：

```
🤖 weak_bear 監控
[📊 auto_select 監控中心]  ← 點擊這裡
```

### 監控面板內容

點擊後展開，顯示：

#### 1. 配置狀態
- ✅ auto_select = true
- ℹ️ active_strategy = null
- 🟢 Paper Trading

#### 2. 微台指設定
| 指標 | 數值 |
|------|------|
| 一點價值 | 10 元 |
| 初始資金 | 50,000 元 |
| 每筆口數 | 2 口 |
| 最大持倉 | 2 口 |

#### 3. weak_bear_trend 參數

**止損/止盈**:
- 止損倍數：1.0 ATR
- 止盈倍數：2.0 ATR
- **盈虧比：2:1**
- 平衡勝率：33.3%

**進場門檻**:
- VWAP 距離：< 0.5 ATR
- 動能門檻：< -8.0
- ADX 上限：< 20.0
- 時間止損：15 分鐘

#### 4. Regime → 策略映射

顯示每個 Regime 下的首選/備選策略：

```
WEAK Regime:
  SHORT: 🥇 weak_bear_trend, 🥈 counter_vwap, 🥉 spring_upthrust
  LONG: 🥇 counter_vwap, 🥈 spring_upthrust
  NEUTRAL: 🥇 range_mean_reversion_v1

SQUEEZE Regime:
  ANY: 🥇 squeeze_fire_scout, 🥈 range_mean_reversion_v1

TREND Regime:
  LONG: 🥇 adaptive_orb_v15, 🥈 trend_continuation_v1
  SHORT: 🥇 adaptive_orb
```

#### 5. 即時 Router Trace

**當前狀態**:
- 當前 Regime: SQUEEZE
- 選擇策略：None (無符合條件)

**候選策略評估**:
```
❌ adaptive_orb_v15: SKIP:ATR_GATE_REJECT:NO_STRUCTURE
❌ squeeze_fire_scout: SKIP:NO_SQUEEZE_FIRE
❌ range_mean_reversion_v1: SKIP:NO_EXTREME_LEVEL
```

**歷史 Trace** (最近 10 次):
| 時間 | Regime | 策略 | 狀態 | 選擇 |
|------|--------|------|------|------|
| 19:25:00 | SQUEEZE | adaptive_orb_v15 | SKIP | |
| 19:25:00 | SQUEEZE | squeeze_fire_scout | SKIP | |
| 19:25:00 | SQUEEZE | range_mean_reversion_v1 | SKIP | |

#### 6. 預期行為

```
當前狀態：SQUEEZE Regime

等待轉換：SQUEEZE → WEAK + SHORT

觸發條件:
- ADX 上升至 15-20 區間
- 價格震盪格局
- Bias 維持 SHORT

自動啟動：weak_bear_trend

進場信號:
- 弱勢反彈失敗後做空
- 止損：1.0 ATR (50 點)
- 止盈：2.0 ATR (100 點)
- 盈虧比：2:1
```

#### 7. 監控清單

```
- [ ] auto_select = true ✅
- [ ] active_strategy = null ✅
- [ ] Paper Trading 模式 ✅
- [ ] 等待 WEAK + Short Regime ⏳
- [ ] weak_bear_trend 進場 ⏳
- [ ] 盈虧比 2:1 驗證 ⏳
```

---

## 🚀 使用方式

### 1. 訪問 Dashboard

```
http://localhost:8500
密碼：5888
```

### 2. 開啟監控面板

1. 在左側邊欄找到 "🤖 weak_bear 監控"
2. 點擊 "📊 auto_select 監控中心" 按鈕
3. 面板展開，顯示完整監控資訊

### 3. 實時監控

**刷新頻率**: Dashboard 會自動刷新 (取決於 st_autorefresh 設置)

**關鍵指標**:
- Regime 變化 (SQUEEZE → WEAK)
- Router 選擇的策略變化
- weak_bear_trend 是否被評估

---

## 📊 解讀監控數據

### 情境 1: SQUEEZE Regime (當前)

```
Regime: SQUEEZE
候選：squeeze_fire_scout, range_mean_reversion_v1
結果：無信號 (等待突破)
```

**解讀**: 
- ✅ 正常行為
- ⏳ 等待 SQUEEZE 轉 WEAK

### 情境 2: WEAK + SHORT Regime

```
Regime: WEAK
Bias: SHORT
候選：weak_bear_trend (首選), counter_vwap, spring_upthrust
結果：weak_bear_trend 進場
```

**解讀**:
- ✅ weak_bear_trend 被正確選擇
- ✅ 自動啟動做空策略
- 📊 準備進場

### 情境 3: weak_bear_trend 進場

```
[WEAK_BEAR_SIGNAL] close=42200 vwap=42250 adx=18.0 mom_velo=-8.0
→ SELL @ 42200, SL=42250, TP=42100
```

**解讀**:
- ✅ 進場條件滿足
- ✅ 止損 50 點 (1.0 ATR)
- ✅ 止盈 100 點 (2.0 ATR)
- ✅ 盈虧比 2:1

---

## 🔧 故障排除

### 問題 1: 監控面板無法載入

**症狀**: 顯示 "⚠️ weak_bear 監控面板載入失敗"

**解決方案**:
```bash
# 檢查 weak_bear_monitor.py 是否存在
ls -la ui/weak_bear_monitor.py

# 檢查語法錯誤
python3 -m py_compile ui/weak_bear_monitor.py

# 重啟 Dashboard
pm2 restart dashboard --update-env
```

### 問題 2: Router Trace 無數據

**症狀**: 顯示 "⏳ 等待 Router Trace 數據..."

**解決方案**:
```bash
# 檢查交易系統是否運行
pm2 status

# 檢查日誌
tail -f logs/pm2-trading-out-11.log | grep RouterTrace

# 如果無數據，重啟交易系統
pm2 restart trading-system --update-env
```

### 問題 3: Regime 顯示不正確

**症狀**: Regime 一直顯示 SQUEEZE，不轉 WEAK

**解決方案**:
```bash
# 檢查 regime 分類邏輯
grep -n "classify_futures_bar_regime" core/futures_bar_regime.py

# 檢查當前 ADX 值
tail -f logs/pm2-trading-out-11.log | grep "adx="

# ADX < 20 才會轉 WEAK
```

---

## 📈 後續優化建議

### 1. 添加歷史績效追蹤

```python
# 記錄每筆 weak_bear_trend 交易
- 進場時間、價格
- 出場時間、價格
- PnL
- 盈虧比
```

### 2. 添加預警功能

```python
# 當 Regime 轉 WEAK 時發送通知
if regime == "WEAK" and bias == "SHORT":
    send_notification("weak_bear_trend 可能進場！")
```

### 3. 添加參數調整介面

```python
# 在 Dashboard 直接調整 weak_bear 參數
stop_atr_mult = st.slider("止損倍數", 0.5, 2.0, value=1.0)
take_profit_atr_mult = st.slider("止盈倍數", 1.0, 3.0, value=2.0)
```

### 4. 添加策略對比

```python
# 對比 weak_bear_trend vs counter_vwap 績效
- 勝率
- 盈虧比
- 最大回撤
- Sharpe Ratio
```

---

## ✅ 總結

### 已完成

- [x] 創建 weak_bear_monitor.py 監控組件
- [x] 整合到 Dashboard (sidebar + 總覽頁面)
- [x] PM2 重啟 Dashboard
- [x] 測試監控面板載入

### 監控面板功能

- ✅ 配置狀態顯示
- ✅ 微台指設定
- ✅ weak_bear 參數
- ✅ Regime → 策略映射
- ✅ 即時 Router Trace
- ✅ 預期行為說明
- ✅ 監控清單

### 使用方式

1. 訪問 http://localhost:8500
2. 點擊 Sidebar 的 "📊 auto_select 監控中心"
3. 查看完整監控資訊
4. 等待 WEAK + Short Regime 出現
5. 監控 weak_bear_trend 進場

---

**Dashboard 更新完成！現在可以實時監控 weak_bear_trend 和 auto_select 系統了！** 🚀

*文檔生成時間：2026-05-07 19:45*
