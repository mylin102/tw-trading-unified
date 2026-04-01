# 日盤 vs 夜盤 Config 分析

## 現況問題

1. **選擇權 config 混雜**：`active_mode: day` 但同時有 V1(日內)/V2(波段)/V3(夜盤) 三個 mode，加上 `night_trading` 區塊，邏輯不清楚
2. **期貨 config 沒有日夜盤區分**：`skip_hours: [21, 22]` 暗示有夜盤意識但沒有完整設計
3. **回測數據不足**：3 天 5m 數據（582 bars）跑出的結果不可靠，單日 session 太短幾乎沒有訊號

## 回測發現

| 測試 | 結果 |
|------|------|
| 全量 3 天（連續） | 期貨 +6,800 / 選擇權 +22,092 |
| 單日 session 獨立 | 0 筆交易（bars 太少，指標暖機不夠） |
| 日盤 vs 夜盤分開 | 無法區分（數據不足） |

## 建議方案

### Phase 1：先用統一參數（現在）

目前數據不足以支持日夜盤分開調參，先用統一 config：

```yaml
# 期貨：統一參數
entry_score: 20
stop_loss_pts: 40
tp1_pts: 80

# 選擇權：統一參數
entry_score: 80
stop_loss_pct: 0.10
tp1_pct: 1.0
```

### Phase 2：收集 30 天數據後分開

需要收集的數據：
- 日盤 5m kbars（08:45~13:45）× 30 天
- 夜盤 5m kbars（15:00~05:00）× 30 天
- 每棒的 score、sqz_on、mom_state

收集方式：monitor 已經在寫 indicator CSV，跑 30 天就有了。

### Phase 3：日夜盤獨立 config

有足夠數據後，分開回測再決定：

```yaml
# 可能的結果（假設）
day_session:
  entry_score: 70      # 日盤波動大，可以低一點
  stop_loss_pct: 0.10
  tp1_pct: 1.0

night_session:
  entry_score: 90      # 夜盤波動小，要更嚴格
  stop_loss_pct: 0.15  # 夜盤流動性差，停損寬一點
  tp1_pct: 0.8         # 夜盤漲幅有限，早停利
```

## 需要清理的 config 項目

| 項目 | 問題 | 建議 |
|------|------|------|
| `exit_strategy` 區塊 | 和 `risk_mgmt` 重複（都有 stop_loss_pct） | 移除 `exit_strategy`，統一用 `risk_mgmt` |
| `tsm_strategy` | threshold 全設 0.0（禁用） | 保留但標註 disabled |
| `fallback_underlying_price: 23000` | 造成假資料（今天的 bug） | 改成 0 或移除 |
| V1/V2/V3 modes | tp1_pct 已統一改成 1.0 | OK |

## 結論

**現在不要分日夜盤 config**，數據不夠。先跑 30 天統一參數收集數據，再用回測決定是否需要分開。
