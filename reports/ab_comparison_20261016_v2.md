# Adaptive A/B v2 雙腿生命週期回放 — 2026-10-16

框架差異驗收：**NOT_MET**；策略提升／部署判定：**NO_GO**。

固定 20260712 near/far 快照；5 個部分覆蓋日曆日期。以下是核准離線 bar proxy，非完整 tick／實盤重建。

## 決策差異

共 807 組成對決策，分母保留所有暖機／缺漏樣本。
雙腿參考線皆有效 577 組；另有 3 次可用性造成的候選差異，未納入5%門檻。
相同假設持倉下 Trend Release 候選差異：52 / 807 = 6.4436%。
實際模擬持倉路徑 action／leg 差異：21 / 807 = 2.6022%。
共同已平倉 spread：27；出場時間／價格改變：7。
成對淨利比較：baseline 勝 5、adaptive 勝 2、平手 20。

兩組同為 SPREAD 且參考線有效的實際決策點：199；共同進場狀態：`{"REFERENCE_NOT_READY": 200, "ENTRY_SCHEDULED": 27, "POSITION_OR_EXIT_PENDING": 293, "COOLDOWN": 110, "NEXT_PAIR_MISSING": 8, "MARGIN_INSUFFICIENT": 169}`。

相同狀態 probe 隔離參考線／門檻效果；portfolio action 包含持倉路徑分歧。僅 block reason 改變不計入決策差異。

## 成本後指標

| 指標 | Baseline | Adaptive | Adaptive − Baseline |
|---|---:|---:|---:|
| 已平倉雙腿 episode 數 | 27.0000 | 27.0000 | 0.0000 |
| 已平倉勝率 % | 25.9259 | 18.5185 | -7.4074 |
| 已平倉平均淨利 TWD | -281.0725 | -298.8521 | -17.7795 |
| 已平倉累計淨利 TWD | -7588.9584 | -8069.0060 | -480.0476 |
| 期末現金 TWD | 92411.0416 | 91930.9940 | -480.0476 |
| 期末估計清算權益 TWD | 92411.0416 | 91930.9940 | -480.0476 |
| 清算權益變化 TWD（含未平倉） | -7588.9584 | -8069.0060 | -480.0476 |
| 清算權益 MDD % | -8.0773 | -9.9562 | -1.8789 |
| 日報酬年化 Sharpe（僅5日期） | -16.9509 | -8.7857 | 8.1652 |
| Trend Release probe 通過數 | 134.0000 | 79.0000 | -55.0000 |

期末清算權益差 / 初始模型資金：-48.0048 bps（非價差報酬率）。

## 固定驗證模型

- EMA：選方案 B，5 根完整 5 分 K、1500 秒有限視窗，最舊 close 為種子、權重 2/(5+1)。缺根或不足 5 根不產生參考線。60 秒方案在 5 分 K 上仍退化，未採用。
- baseline／adaptive 均要求雙腿同方向、price-reference 距離至少 0.1%、連續 2 根確認，沿用 Renko＋ADL＋參考線 2-of-3 仲裁。SPREAD 使用既有 entry-side 映射：LONG NEAR／SHORT FAR 只在看多確認後釋放 FAR；SINGLE_LEG 另外允許確認反轉出場。
- adaptive 額外要求雙腿 alpha≥0.3、adaptive-VWAP 位移至少 0.1%，且方向與 VWAP 一致。故 A/B 同時改變參考線與確認門檻，不能把差異全歸因 EMA。
- 共享假設 LONG NEAR / SHORT FAR 進場；兩組均 flat 且滿足冷卻／資金條件才建立下一組。兩組分別維護 SPREAD→SINGLE_LEG→FLAT，不重疊計算 episode。
- 進出場均在決策後下一根完整配對 K 線 open，採 adverse slippage；缺根不把舊訊號延至後續開盤成交。未平倉部位與未成交 pending 不偽造終點成交。
- 直接呼叫既有 evaluate_lifecycle_actions：保留 hard stop／timeout、Policy J、Trend Release、protective release、reversal、trail 優先順序。門檻只影響 trend，不能擋風控。
- marks_live=False：保留 closed-bar 下 Policy J release-intercept 拒絕；giveback／single-leg 路徑使用原函式。peak 是 bar close 代理，無法驗證 tick 確認。
- 既有 lifecycle Policy J 決策內部固定 10 TWD/點、92 TWD friction，原樣保留並限制 product point_value=10。成交帳則另按 product 設定計費／稅／滑價，兩者未混為真實成本。
- 初始研究資金 100000 TWD；屬假設持倉研究，不是 paper 執行授權或既有 40000 TWD runtime 資金上限變更。

## 成交量校準

| 腿 | 有效 / 未知 | causal Z 中位數 | EXTREME % | HIGH+EXTREME % |
|---|---:|---:|---:|---:|
| near | 696 / 111 | -0.2456 | 6.6092 | 15.6609 |
| far | 687 / 120 | -0.2814 | 6.9869 | 13.2460 |

每腿／時段僅用過去最多120根完整5分量，至少30根才評分；當前量先評分再加入校準。全快照均值／標準差僅列診斷，不回填歷史訊號。原 ZSCORE_PARAMS 未改，避免同週訓練同週驗證前視。

## 路徑與限制

```json
{
  "baseline": {
    "actions": {
      "HOLD": 736,
      "ENTRY": 27,
      "TIMEOUT": 20,
      "TREND_RELEASE": 12,
      "TRAIL": 11,
      "COMBINED_EXIT": 1
    },
    "exit_reasons": {
      "TIMEOUT": 29,
      "TREND_RELEASE": 12,
      "TRAIL": 11,
      "COMBINED_EXIT": 2
    },
    "probe_block_reasons": {
      "LEG_DIRECTION_DIVERGENCE": 22,
      "ENTRY_SIDE_DIRECTION_MISMATCH": 352,
      "PRICE_DISTANCE_BELOW_THRESHOLD": 61,
      "DIRECTION_NOT_CONFIRMED": 10,
      "ADL_RENKO_WARMUP_OR_GAP": 78,
      "CONFIRMED": 134,
      "CONFIRMATION_GAP": 26,
      "CONFIDENCE_BELOW_THRESHOLD": 42,
      "DIVERGENCE_OR_INSUFFICIENT": 57,
      "INSUFFICIENT_SAME_DIRECTION": 25
    },
    "open_episode": false,
    "pending_unfilled": false,
    "data_gaps": 14
  },
  "adaptive": {
    "actions": {
      "HOLD": 737,
      "ENTRY": 27,
      "TIMEOUT": 23,
      "TREND_RELEASE": 10,
      "TRAIL": 8,
      "COMBINED_EXIT": 1,
      "STOPLOSS": 1
    },
    "exit_reasons": {
      "TIMEOUT": 33,
      "TREND_RELEASE": 10,
      "TRAIL": 8,
      "COMBINED_EXIT": 2,
      "STOPLOSS": 1
    },
    "probe_block_reasons": {
      "REFERENCE_UNAVAILABLE": 230,
      "REFERENCE_SHIFT_BELOW_THRESHOLD": 31,
      "ENTRY_SIDE_DIRECTION_MISMATCH": 231,
      "ALPHA_BELOW_THRESHOLD": 43,
      "CONFIRMED": 79,
      "CONFIDENCE_BELOW_THRESHOLD": 29,
      "PRICE_DISTANCE_BELOW_THRESHOLD": 97,
      "DIVERGENCE_OR_INSUFFICIENT": 23,
      "DIRECTION_NOT_CONFIRMED": 4,
      "LEG_DIRECTION_DIVERGENCE": 13,
      "ADL_RENKO_WARMUP_OR_GAP": 11,
      "INSUFFICIENT_SAME_DIRECTION": 16
    },
    "open_episode": false,
    "pending_unfilled": false,
    "data_gaps": 15
  }
}
```

尚缺來源合約／成交量單位驗真、OI／品質事件、tick MID／BBO、實際進場紀錄與完整7/10日盤；未做樣本外驗證。決策差異不等於績效改善。

來源／執行參數／雜湊：`model_manifest.json`；逐筆決策／成交：每日 baseline/adaptive JSONL、`portfolio.json`、`replay_trace.jsonl`。

來源基準 commit：`3ec716584c994cb82c8925c197001705286b72a1`。
