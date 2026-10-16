# 成交量校準診斷 — 2026-10-16

資料僅為固定 20260712 快照的 7/6～7/10 部分樣本，不能稱為完整 2026-07 母體統計。

原回放 98.63% 是 HIGH+EXTREME 合計，EXTREME 單獨 96.57%。

## 統計比較

| 腿 / 時段 | N | 5分量均值 | 標準差 | 舊均值 / std | 舊 Z 中位數 | 舊 EXTREME % | causal N | causal Z 中位數 | causal EXTREME % |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|
| far / afternoon | 36 | 170.528 | 98.954 | {'mean': 396, 'std': 96} | -2.630 | 0.000 | 6 | -0.587 | 0.000 |
| far / morning | 108 | 268.546 | 140.293 | {'mean': 467, 'std': 121} | -1.860 | 1.852 | 78 | -0.030 | 10.256 |
| far / night | 439 | 126.207 | 126.236 | {'mean': 210, 'std': 60} | -2.150 | 7.517 | 409 | -0.329 | 5.623 |
| far / off_segment | 224 | 179.589 | 151.940 | 未定義 | N/A | N/A | 194 | -0.278 | 8.763 |
| near / afternoon | 36 | 2397.194 | 1029.313 | {'mean': 396, 'std': 96} | 19.120 | 100.000 | 6 | -0.632 | 0.000 |
| near / morning | 108 | 4728.954 | 1872.633 | {'mean': 467, 'std': 121} | 34.430 | 100.000 | 78 | -0.174 | 5.128 |
| near / night | 528 | 1672.794 | 1680.440 | {'mean': 210, 'std': 60} | 14.608 | 86.553 | 498 | -0.349 | 6.024 |
| near / off_segment | 232 | 2567.642 | 1971.483 | 未定義 | N/A | N/A | 202 | -0.338 | 5.941 |

## 單位與處理決定

來源分鐘資料的數值檢查（仍無單位認證）：

```json
{
  "data/tmf_near_20260712.csv": {
    "source_minutes": 4561,
    "volume_integral_pct": 100.0,
    "amount_over_volume_close_median": 0.9999988796547544,
    "unit_verified": false
  },
  "data/tmf_far_20260712.csv": {
    "source_minutes": 4418,
    "volume_integral_pct": 100.0,
    "amount_over_volume_close_median": 1.0,
    "unit_verified": false
  }
}
```

CSV 欄位只有 Volume／Amount，沒有資料字典、合約代碼或原始供應商單位證明。Volume 為來源數值，重採樣按五個完整分鐘相加；沒有乘以點值或價格。僅憑欄位不能確認它是口數、是否雙計或是否與舊校準同一合約。

因此不把全快照均值直接寫回 production ZSCORE_PARAMS。v2 採每腿／時段 prior-only rolling mean/std；先評分、再加入當根量；至少30根，最多120根。off_segment 另分組，未知／零變異保留空值。

全快照統計僅供診斷，未參與校準；rolling 校準只調整訊號分布，沒有用 PnL 選參。較低異常率不證明資料正確或分布常態，仍需驗證來源單位與跨期穩定性。
