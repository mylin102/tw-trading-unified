# 精簡有效策略 — TMF Elite Strategies

## 現況分析 (2026 Q1 回測數據)

### 所有策略表現總覽

| 策略 | Profit Factor | Win Rate | MaxDD | Total PnL | 交易數 | 狀態 |
|------|--------------|----------|-------|-----------|--------|------|
| **Counter + VWAP** | **1.95** | **40.7%** | **-7.2%** | **+32,285** | 113 | ✅ 唯一有效 |
| PSAR Breakout | 1.42 | 35% | -12% | +18,500 | 67 | ⚠️ 可接受 |
| Breakout (原始) | 1.02 | 27.25% | -25.8% | +4,354 | 444 | ❌ 假突破太多 |
| Counter (無 VWAP) | 0.00-0.52 | 4-6% | -40% | -40,905 | 89 | ❌  catastrophically 失敗 |
| Night Session (全部) | 0.00-0.20 | <10% | -37% | -37,000 | 156 | ❌ 夜盤流動性不足 |
| Options (score=90) | 0.08 | <5% | -70% | -280,000 | 45 | ❌ 進場太頻繁 |
| Options (score=70-80) | 1.25 | 30% | -10% | +22,092 | 30 | ⚠️ 勉強可用 |

### 根因分析

**為什麼大部分策略賠錢：**

1. **假突破泛濫** (Breakout PF=1.02)
   - Squeeze Fire 後 27% 勝率 = 每 4 筆交易 3 筆虧損
   - 台指期 5m 時間框架均值回歸特性強，假突破多
   - 缺乏量能確認，純價格突破不可靠

2. **夜盤流動性不足** (Night Session 全部賠錢)
   - 15:00~05:00 成交量稀疏，技術指標失靈
   - 受美股影響大於台股本身因素
   - VWAP 行為與日盤完全不同

3. **出場機制決定成敗** (Counter 有/無 VWAP 差異巨大)
   - Counter 有 VWAP: PF=1.95, MaxDD=-7.2%
   - Counter 無 VWAP: PF=0.00, MaxDD=-40%
   - **VWAP 出場不是選項，是核心獲利機制**

4. **參數過度敏感** (Options score=90 vs 70)
   - 進場分數 90 → 虧損 280,000
   - 進場分數 70 → 獲利 22,092
   - 甜蜜點極窄，容錯率低

---

## 精簡策略 (去蕪存菁)

### 策略篩選標準

✅ **保留條件：**
- Profit Factor >= 1.4 (扣除成本後仍獲利)
- MaxDD <= -15% (風險可控)
- Win Rate >= 30% (不需要高勝率，但需合理)
- 交易數 >= 30 (統計意義)

❌ **剔除條件：**
- 夜盤策略 (流動性不足)
- 純價格突破 (假突破太多)
- 參數過度敏感 (容錯率低)
- 依賴單一市場狀態 (regime-specific)

---

## ELITE STRATEGY #1: Counter-VWAP (核心策略)

**回測數據:** PF=1.95, WR=40.7%, MaxDD=-7.2%, PnL=+32,285

### 為什麼有效

1. **均值回歸是台指期本質**
   - 台指期 5m 時間框架呈現強烈均值回歸
   - VWAP 是機構參考價，價格天然傾向回歸

2. **Squeeze Failure 是可靠信號**
   - 偵測「突破失敗」而非「突破成功」
   - 失敗突破 = 假突破 = 反向進場點

3. **VWAP 出場是核心**
   - 不是停損/停利出場
   - 是「價格回歸均值」出場
   - 與均值回歸邏輯完全一致

### 進場邏輯

```python
# 偵測 Squeeze Fire (波動率壓縮釋放)
squeeze_fire = (previous_bars_sqz_on) and (current_bar_sqz_off)

if squeeze_fire:
    # 記錄 Fire 當下的價格區間
    fire_high = max(high of last N bars)
    fire_low = min(low of last N bars)
    
    # 等待確認突破失敗
    if direction == LONG:
        failure = (
            price fails to make new high within 5 bars AND
            momentum reverses (mom_state <= 1) AND
            price rejected at VWAP
        )
        if failure:
            enter SHORT (反向)
            stop_loss = fire_high + ATR * 2.0
            target = VWAP (均值回歸)
    
    if direction == SHORT:
        failure = (
            price fails to make new low within 5 bars AND
            momentum reverses (mom_state >= 2) AND
            price rejected at VWAP
        )
        if failure:
            enter LONG (反向)
            stop_loss = fire_low - ATR * 2.0
            target = VWAP (均值回歸)
```

### 關鍵參數

| 參數 | 值 | 說明 |
|------|-----|------|
| confirm_bars | 5 | 等待 5 根 K 棒確認失敗 |
| atr_sl_mult | 2.0 | 停損 = 2x ATR (寬停損) |
| exit_on_vwap | true | **必須啟用** |
| auto_regime | true | 自動判斷趨勢/盤整 |

### 適用市場狀態

- ✅ 盤整市場 (70% 時間)
- ✅ 趨勢反轉點
- ⚠️ 弱勢趨勢 (可能提前出場)
- ❌ 強勢趨勢 (可能連續假信號)

---

## ELITE STRATEGY #2: PSAR Breakout (輔助策略)

**回測數據:** PF=1.42, WR=35%, MaxDD=-12%, PnL=+18,500

### 為什麼有效

1. **PSAR 是動態支撐壓力**
   - 比固定價格突破可靠
   - 自動跟隨趨勢調整

2. **50MA 過濾假信號**
   - 價格 > 50MA 才做多
   - 價格 < 50MA 才做空
   - 避免逆勢交易

3. **ADX 過濾弱勢趨勢**
   - ADX >= 15 才進場
   - 確保有足夠趨勢強度

### 進場邏輯

```python
# PSAR 翻轉偵測
psar_flip_long = (not psar_long_prev) and (psar_long_now)
psar_flip_short = (not psar_short_prev) and (psar_short_now)

# 進場條件
if psar_flip_long:
    if price > SMA50 and ADX >= 15:
        enter LONG
        stop_loss = ATR * 2.0
        # 移動停損跟隨 PSAR
        
if psar_flip_short:
    if price < SMA50 and ADX >= 15:
        enter SHORT
        stop_loss = ATR * 2.0
        # 移動停損跟隨 PSAR
```

### 關鍵參數

| 參數 | 值 | 說明 |
|------|-----|------|
| acceleration | 0.02 | PSAR 初始加速度 |
| acceleration_max | 0.2 | PSAR 最大加速度 |
| sma_length | 50 | 趨勢過濾均線 |
| atr_mult | 2.0 | 停損倍數 |
| min_adx | 15 | 最小趨勢強度 |

### 適用市場狀態

- ✅ 趨勢啟動初期
- ✅ 盤整突破後
- ⚠️ 震盪行情 (可能頻繁停損)
- ❌ 盤整無突破 (無信號)

---

## ELITE STRATEGY #3: Volume-Filtered Squeeze (品質提升版)

**理論基礎:** 原始 Breakout PF=1.02 → 加入量能過濾後預估 PF=1.3+

### 為什麼加入量能

1. **假突破特徵:** 價格突破但量能不足
2. **真突破特徵:** 價格突破 + 量能爆發 (機構參與)
3. **量能過濾:** 只交易「有成交量支撐」的突破

### 進場邏輯

```python
# 原始 Squeeze 信號
sqz_buy = (not sqz_on) and score >= entry_score and mom_state >= 2

# 量能過濾
vol_ma = SMA(Volume, 20)
vol_spike = current_volume > vol_ma * 1.5

# 組合信號
if sqz_buy and vol_spike:
    enter LONG
    stop_loss = ATR * 1.5
    target = VWAP or trailing stop
```

### 關鍵參數

| 參數 | 值 | 說明 |
|------|-----|------|
| vol_multiplier | 1.5 | 量能 > 1.5x 均量 |
| entry_score | 20 | 進場分數門檻 |
| atr_mult | 1.5 | 停損倍數 |
| regime_filter | mid | 趨勢過濾 |

### 適用市場狀態

- ✅ 趨勢初期 (量能確認)
- ✅ 突破行情 (真突破過濾)
- ⚠️ 盤整 (信號減少)
- ❌ 無量行情 (無信號)

---

## 策略組合使用規則

### 市場狀態自動判斷

```python
def detect_market_regime(df, lookback=20):
    """
    判斷當前市場狀態，決定使用哪個策略
    """
    # 計算趨勢強度
    bullish_align_count = count_bullish_alignments(df[-lookback:])
    
    # 盤整市場: 趨勢頻繁翻轉
    if bullish_align_count >= 4:
        return "RANGING" → 使用 Counter-VWAP
    
    # 趨勢市場: 趨勢穩定
    elif bullish_align_count <= 1:
        return "TRENDING" → 使用 PSAR Breakout
    
    # 過渡期: 可能突破
    else:
        return "TRANSITION" → 使用 Volume-Filtered Squeeze
```

### 策略切換邏輯

| 市場狀態 | 使用策略 | 預期勝率 | 預期 PF |
|---------|---------|---------|---------|
| 盤整 (70% 時間) | Counter-VWAP | 40-45% | 1.8-2.0 |
| 趨勢初期 (15%) | PSAR Breakout | 35-40% | 1.3-1.5 |
| 突破行情 (10%) | Vol-Filtered Squeeze | 35-40% | 1.2-1.4 |
| 夜盤 (不交易) | **休息** | - | - |

### 資金分配

```yaml
# 單一策略最大風險: 總資金 2%
# 同時最多 1 個持倉 (避免相關性風險)

risk_per_trade: 0.02          # 2% 風險
max_positions: 1              # 單一持倉
max_daily_trades: 5           # 每日最多 5 筆
max_daily_loss_pct: 0.05      # 每日最大虧損 5%
```

---

## 嚴格風控規則

### 進場前檢查 (SDD Rule 2.3)

```python
def pre_entry_check(state, price, lots):
    """所有策略進場前必須通過"""
    
    # 1. 持倉檢查
    if position != 0:
        return False, "Already in position"
    
    # 2. 價格合理性
    if price <= 0:
        return False, "Invalid price"
    
    if not is_price_reasonable(price, underlying):
        return False, "Price unreasonable"
    
    # 3. 保證金檢查
    if not margin_sufficient(price, lots):
        return False, "Insufficient margin"
    
    # 4. 同根 K 棒檢查
    if traded_this_bar():
        return False, "Already traded this bar"
    
    # 5. 夜盤過濾 (15:00~05:00 不交易)
    hour = datetime.now().hour
    if hour >= 15 or hour < 5:
        return False, "Night session - no trading"
    
    # 6. 開盤 30 分鐘過濾 (避免開盤混亂)
    if is_first_30_minutes():
        return False, "Opening range - no trading"
    
    return True, "OK"
```

### 出場規則 (SDD Rule 3)

```python
def exit_position(position, entry_price, current_price, reason):
    """所有策略出場必須遵守"""
    
    # 1. 先歸零再記錄
    qty = position
    position = 0
    
    # 2. 使用市場價 (不是停損價)
    exit_price = current_price
    
    # 3. 計算 PnL (包含所有成本)
    pnl = calculate_pnl_with_fees(
        entry_price, exit_price, qty,
        broker_fee, exchange_fee, tax
    )
    
    # 4. 記錄交易
    log_trade(qty=qty, pnl=pnl, reason=reason)
    
    return pnl
```

### 停損規則

```python
# Counter-VWAP: 寬停損 (2x ATR)
stop_loss = entry_price ± (ATR * 2.0)

# PSAR Breakout: 跟隨 PSAR 移動停損
stop_loss = PSAR_value

# Volume-Filtered Squeeze: 標準停損
stop_loss = entry_price ± (ATR * 1.5)

# 所有策略:
# - 浮盈 >= 50 pts → 啟動追蹤停損
# - 追蹤停損距離: 30 pts
# - 保本停損: 進場價 + 10 pts (覆蓋手續費)
```

---

## 剔除策略清單 (永不使用)

| 策略 | 原因 | PF | MaxDD |
|------|------|-----|-------|
| Night Short Only | 夜盤流動性不足 | 0.04 | -37% |
| Breakout (原始) | 假突破太多 | 1.02 | -25.8% |
| VWAP Bounce | 均值回歸信號不穩定 | 0.85 | -18% |
| Momentum Burst | Z-Score 太敏感 | 0.92 | -22% |
| Cumulative Delta | Delta 估計不準確 | 0.78 | -28% |
| Volume Reversal | 信號太少 | 0.88 | -15% |
| Gap Reversal | 跳空後趨勢延續 | 0.95 | -20% |

---

## 回測驗證計畫

### V-Model Level 1: 單元測試

```python
# 測試每個策略信號產生邏輯
test_counter_vwap_detects_failure()
test_psar_flip_long_with_sma_filter()
test_volume_filter_rejects_low_vol_breakout()
```

### V-Model Level 2: 整合測試

```python
# 測試完整交易循環
test_counter_vwap_entry_to_vwap_exit()
test_psar_entry_to_trailing_stop_exit()
test_regime_switch_no_orphan_position()
```

### V-Model Level 3: 系統測試

```bash
# 用 2026 Q1 數據回測
python3 scripts/backtest_elite_strategies.py

# 預期結果:
# - Counter-VWAP: PF >= 1.8, MaxDD <= -10%
# - PSAR: PF >= 1.3, MaxDD <= -15%
# - Vol-Squeeze: PF >= 1.2, MaxDD <= -15%
# - 組合: PF >= 1.5, MaxDD <= -12%
```

### V-Model Level 4: UAT Checklist

- [ ] 所有策略 PF > 1.2
- [ ] 所有策略 MaxDD < -15%
- [ ] 所有策略 Win Rate > 30%
- [ ] 夜盤完全不交易
- [ ] 開盤 30 分鐘過濾
- [ ] 每筆交易 PnL 包含手續費
- [ ] 停損使用市場價
- [ ] 持倉歸零後才記錄

---

## 實作優先順序

| 優先級 | 任務 | 預計影響 |
|--------|------|---------|
| **P0** | Counter-VWAP 策略 | 核心獲利來源 (PF=1.95) |
| **P0** | 夜盤交易禁止 | 避免 catastrophic loss |
| **P1** | PSAR Breakout | 輔助趨勢策略 |
| **P1** | 量能過濾 | 提高信號品質 |
| **P2** | 市場狀態自動切換 | 最佳化策略選擇 |
| **P2** | 開盤 30 分鐘過濾 | 減少假信號 |

---

## 總結

### 關鍵發現

1. **只有 1 個策略真正有效:** Counter-VWAP (PF=1.95)
2. **假突破是最大敵人:** Breakout PF=1.02, 27% 勝率
3. **夜盤是殺手:** 所有夜盤策略 catastrophically 失敗
4. **出場決定成敗:** VWAP 出場是 Counter 策略核心

### 行動計畫

1. ✅ **保留 3 個策略:** Counter-VWAP, PSAR, Vol-Squeeze
2. ✅ **剔除 7 個策略:** 全部賠錢或風險太高
3. ✅ **禁止夜盤交易:** 避免流動性風險
4. ✅ **強制量能過濾:** 提高突破信號品質
5. ✅ **自動市場狀態判斷:** 動態切換策略

### 預期結果

| 指標 | 目前 (全部策略) | 精簡後 (Elite) |
|------|----------------|---------------|
| Profit Factor | 1.02 | **1.5+** |
| Win Rate | 27% | **35-40%** |
| MaxDD | -25.8% | **<-12%** |
| 交易數 | 444 (太多) | **~150 (合理)** |
| 年化報酬 | +4,354 | **+50,000+** |

---

**建立日期:** 2026-04-07
**基於數據:** 2026 Q1 回測 (2026-01-01 ~ 2026-03-31)
**狀態:** 待實作與驗證
