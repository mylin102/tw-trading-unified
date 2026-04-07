# 精英策略快速參考 — Elite Strategies Quick Reference

## 一秒摘要

**3 個精英策略** (從 10 個精簡而來)，**83 個測試全部通過** ✅

| 策略 | PF | 適用市場 | 關鍵 |
|------|-----|---------|------|
| Counter-VWAP | 1.95 | 盤整 (70%) | VWAP 出場是核心 |
| PSAR Breakout | 1.42 | 趨勢 (15%) | ADX >= 15 |
| Vol-Squeeze | ~1.3 | 突破 (10%) | Volume > 1.5x |

**禁止夜盤交易** (15:00~05:00) — 歷史數據 catastrophic loss ❌

---

## 快速啟動

```bash
# 1. 驗證策略邏輯
python3 scripts/validate_elite_strategies.py

# 2. 執行完整測試
python3 -m pytest tests/ -v

# 3. 啟動系統 (paper mode)
python3 main.py --dry-run

# 4. 啟動系統 (live mode)
python3 main.py
```

---

## 策略選擇 (自動)

系統會根據市場狀態自動切換策略:

```python
盤整市場 (bullish_align 翻轉 >=4 次/20 bars)
  → Counter-VWAP

趨勢市場 (bullish_align 翻轉 <=1 次/20 bars)
  → PSAR Breakout

過渡期 (2-3 次翻轉)
  → Vol-Squeeze

夜盤 (15:00~05:00)
  → 禁止交易 ❌
```

---

## 關鍵參數速查

### Counter-VWAP (核心)

```yaml
counter_mode:
  enabled: true
  auto_regime: true
  confirm_bars: 5        # 等待 5 根確認失敗
  atr_sl_mult: 2.0       # 寬停損
  exit_on_vwap: true     # ⚠️ 必須啟用
```

### PSAR Breakout (趨勢)

```yaml
psar_breakout:
  acceleration: 0.02
  sma_length: 50
  min_adx: 15            # 捕捉趨勢初期
  atr_mult: 2.0
```

### Vol-Squeeze (品質)

```yaml
vol_squeeze:
  vol_multiplier: 1.5    # 量能門檻
  entry_score: 20
  atr_mult: 1.5
```

---

## 風控規則

### 進場前 ✅

- [ ] position == 0
- [ ] margin_sufficient()
- [ ] price > 0
- [ ] not same_bar
- [ ] 非夜盤 (hour NOT in [15-05])
- [ ] 非開盤 30 分鐘內

### 出場規則

```python
qty = position      # 1. Capture
position = 0        # 2. Zero first
log_trade(qty=qty)  # 3. Log after
```

### 停損設定

```
Counter-VWAP:   entry ± (ATR * 2.0)    # 寬
PSAR:           跟隨 PSAR 移動         # 動態
Vol-Squeeze:    entry ± (ATR * 1.5)    # 標準

通用:
- 浮盈 >= 50 pts → 追蹤停損
- 追蹤距離: 30 pts
- 保本: entry + 10 pts
```

---

## 淘汰策略清單 ❌

這些策略**已經移除，永不使用**:

| 策略 | PF | 原因 |
|------|-----|------|
| Night Short Only | 0.04 | 夜盤流動性不足 |
| Breakout (原始) | 1.02 | 假突破太多 |
| Counter (無 VWAP) | 0.00 | VWAP 是核心 |
| VWAP Bounce | 0.85 | 信號不穩 |
| Momentum Burst | 0.92 | 太敏感 |
| Cumulative Delta | 0.78 | 估計不準 |
| Volume Reversal | 0.88 | 信號太少 |

---

## 檔案位置

```
strategies/futures/elite_strategies.py    # 核心策略
config/futures.yaml                        # 配置
scripts/validate_elite_strategies.py       # 驗證
ELITE_STRATEGIES.md                        # 完整文檔
ELITE_IMPLEMENTATION_SUMMARY.md            # 實作總結
```

---

## 測試命令

```bash
# 快速驗證
python3 scripts/validate_elite_strategies.py

# 完整測試
python3 -m pytest tests/ -v

# 預期: 83 passed
```

---

## 風險警告

⚠️ **過去績效不保證未來結果**

- Counter-VWAP PF=1.95 基於 2026 Q1 數據
- 建議先用 paper mode 驗證 2-4 週
- 嚴格遵守 2% 風險規則
- 持續監控實盤表現

---

**最後更新:** 2026-04-07
**狀態:** ✅ 完成，待實盤驗證
