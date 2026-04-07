# 實盤交易轉換指南 — Live Trading Progression

## 三階段轉換流程

```
Phase 1: Paper 觀察 (1-2 週)
  ↓
Phase 2: 小額實盤測試 (1 口, 1 週)
  ↓
Phase 3: 正常交易 (2 口+)
```

---

## Phase 1: Paper 觀察期 (目前階段)

### 進入條件
- ✅ 精英策略已部署 (Counter-VWAP, PSAR, Vol-Squeeze)
- ✅ 所有 83 個單元測試通過
- ✅ 系統可正常運行 (main.py 無 crash)

### 觀察指標

| 指標 | 門檻 | 說明 |
|------|------|------|
| **最小交易數** | ≥ 10 筆 | 統計意義 |
| **Profit Factor** | ≥ 1.3 | 扣除成本後獲利 |
| **最大虧損** | ≥ -15% | 風險可控 |
| **勝率** | ≥ 30% | 不需要高勝率 |
| **觀察天數** | ≥ 7 天 | 涵蓋不同市場狀態 |
| **停損正常** | 100% 觸發 | 無漏單 |
| **重複進場** | 0 次 | position guard 正常 |

### 檢查指令

```bash
# 查看交易記錄
tail -20 logs/unified.log | grep -E "BUY|SELL|EXIT"

# 檢查 PnL
cat data/trades_log.csv | tail -20

# 檢查持倉狀態
python3 -c "
import yaml
cfg = yaml.safe_load(open('config/futures.yaml'))
print(f'Mode: {\"LIVE\" if cfg[\"live_trading\"] else \"PAPER\"}')"
```

---

## Phase 2: 小額實盤測試

### 進入條件 (全部必須滿足)

```yaml
# 自動檢查 (Dashboard 會顯示通知)
paper_trades: >= 10
profit_factor: >= 1.3
max_drawdown: >= -15  # 不小於 -15%
win_rate: >= 30
observation_days: >= 7
stop_loss_hit_rate: 100  # 所有停損都正常觸發
duplicate_entries: 0     # 無重複進場
```

### 保守實盤配置

```yaml
# config/futures.yaml
live_trading: true

trade_mgmt:
  lots_per_trade: 1      # 最小口數
  max_positions: 1       # 單一持倉 (降低風險)

risk_mgmt:
  max_daily_loss: 0.02   # 每日最大虧損 2%
  stop_loss_pts: 60      # 固定停損 60 pts
  atr_multiplier: 1.5    # ATR 停損 1.5x

monitoring:
  poll_interval_secs: 15  # 更頻繁檢查 (原 30s)
```

### 風險控制

| 項目 | 設定 | 說明 |
|------|------|------|
| 單口保證金 | ~17,000 TWD | TMF 1 口 |
| 單筆最大風險 | 60 pts × 50 = 3,000 TWD | 停損 60 pts |
| 每日最大虧損 | 2% × 40,000 = 800 TWD | 觸發後停止交易 |
| 最大持倉 | 1 口 | 不允許加碼 |

### 監控清單

每天檢查：
- [ ] 進出場是否正確記錄
- [ ] 停損是否正常觸發
- [ ] PnL 計算是否正確 (含手續費)
- [ ] 是否有重複進場
- [ ] 夜盤是否正常運作

### 退出條件 (任何一項觸發即退回 Paper)

- 連續 3 筆虧損
- 單日虧損 > 800 TWD
- 系統 crash 超過 2 次
- 發現任何 duplicate entry
- 停損未正常觸發

---

## Phase 3: 正常交易

### 進入條件

- Phase 2 穩定運行 ≥ 5 個交易日
- 累積 Profit Factor ≥ 1.3
- 無重大異常

### 正常配置

```yaml
live_trading: true

trade_mgmt:
  lots_per_trade: 2      # 恢復正常口數
  max_positions: 2       # 允許 2 口

risk_mgmt:
  max_daily_loss: 0.03   # 放寬到 3%
```

---

## 實盤就緒度檢查 (Dashboard 自動執行)

Dashboard 會在設定頁顯示即時就緒度：

```
🟢 準備就緒 (8/8 項檢查通過)
  或
🟡 觀察中 (5/8 項通過, 還需 3 天)
  或
🔴 尚未準備 (2/8 項通過, 需要更多數據)
```

### 檢查項目

| # | 檢查 | 通過標準 |
|---|------|---------|
| 1 | 最小交易數 | ≥ 10 筆 |
| 2 | Profit Factor | ≥ 1.3 |
| 3 | 勝率 | ≥ 30% |
| 4 | 最大虧損 | ≥ -15% |
| 5 | 觀察天數 | ≥ 7 天 |
| 6 | 停損觸發率 | 100% |
| 7 | 無重複進場 | 0 次 |
| 8 | 選擇權 PnL 正確 | 含手續費 |

---

## 緊急處理

### 如果實盤出問題

```bash
# 1. 立刻切換回 Paper
# Dashboard → 設定 → live_trading 取消勾選

# 2. 手動平倉
python3 -c "
from core.shioaji_session import get_api
api = get_api()
positions = api.list_positions(api.futopt_account)
for p in positions:
    # 反向下單平倉
    action = 'Sell' if str(p.direction) == 'Buy' else 'Buy'
    api.place_order(p.contract, {
        'action': action,
        'quantity': p.quantity,
        'price_type': 'MKT',
    })
"

# 3. 檢查日誌
tail -100 logs/unified.log | grep -E "error|crash|fail"
```

### 緊急聯絡

- Shioaji 技術支援: support@shioaji.com.tw
- 永豐期貨客服: 0800-588-888

---

## 歷史記錄

| 日期 | 事件 | 結果 |
|------|------|------|
| 2026-04-07 | 精英策略部署完成 | Paper mode 運行中 |
| 2026-04-07 | 期貨首次進場 (Paper) | LONG @ 33464, +22 pts |
| 2026-04-07 | 選擇權虧損修復 | P0/P1 修復完成 |
| TBD | Phase 2 開始 | 待觀察滿 7 天 |

---

**建立日期:** 2026-04-07
**狀態:** Phase 1 - Paper 觀察期
