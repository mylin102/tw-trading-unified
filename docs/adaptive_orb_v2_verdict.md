# adaptive_orb v1 vs v2 — 回測判決書

**日期**: 2026-05-02
**數據**: TXFR1 5m bars (2023-04-12 → 2026-04-23, 824K bars)

## 核心發現

| 指標 | v1 (adaptive_orb) | v2 (adaptive_orb_v2) |
|------|-------------------|---------------------|
| PF | **1.10** | 1.04 |
| CAGR | **61.95%** | 17.81% |
| MFE/MAE | **1.74** | 1.02 |
| Avg hold (bars) | **236.5** | 15.0 |
| 交易數 | 3,012 | 7,004 |
| Net PF (含費用) | 0.96 | 0.56 |

## 根因分析

v2 的 entry 邏輯不是主問題。**致命點有三個**：

1. **退出太緊** (1.5x ATR SL / 3x ATR TP)
   持倉 bar 從 236 → 15，趨勢還沒跑完就被 stop out
   MFE/MAE ratio 從 1.74 → 1.02，edge 幾乎砍半

2. **交易數爆炸** (3,012 → 7,004)
   更多交易 = 更多費用 (每筆 8 pts × 200 = $1,600)
   費用吃掉所有 Pf 優勢

3. **EARLY_BREAKOUT 賠錢** (−$35K / 3K trades)
   Scout entry 勝率 35.6% 但 avg −$11.7
   雖然單筆小賠，但 3,023 筆累積成一筆大窟窿

## v2 Entry-Type 細拆

| Entry Type | 次數 | Total PnL | Avg/Trade | WR |
|-----------|------|-----------|-----------|-----|
| CONFIRMED_BREAKOUT | 3,981 | +$670,600 | +$168.5 | 36.5% |
| EARLY_BREAKOUT | 3,023 | −$35,340 | −$11.7 | 35.6% |

## 結論

**保留 v1 為主力版本。v2 僅供實驗，且需關閉 EARLY 並放寬 exit。**

## v1.5 路線 (next)

```
adaptive_orb_v1
+ ATR breakout 欄位 (從 v2 拿來用)
+ router / kill switch (已實作在 Strategy Router v2)
+ confirmed breakout filter
+ v1 長持倉 exit 邏輯保留 (236 bars avg hold)
- 不加入 scout early breakout
```

核心原則：**不要為了提高勝率犧牲 MFE/MAE ratio。**

## v2 變更 (本次)

- `ENABLE_EARLY_BREAKOUT = False` (default)
- `EXIT_CONFIG` 放寬到 2.0x ATR SL / 4.0x ATR TP
- 文件化回測判決
