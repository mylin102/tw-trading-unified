# A/B 離線代理回放比較 — 2026-10-16

結論：**NO_GO，僅完成核准代理模型的回放驗證，不構成 MTS 實際策略績效或部署依據。**

固定 20260712 near/far 快照；2026-07-06～2026-07-10，共 807 組決策，共同可評估結果 537 組。所有缺漏結果保留 null。

## 指標

| 指標 | Baseline | Adaptive | Adaptive − Baseline |
|---|---:|---:|---:|
| 模擬交易數 | 533.0000 | 533.0000 | 0.0000 |
| 交易勝率 % | 48.7805 | 48.7805 | 0.0000 |
| 共同結果勝率 %（含 flat） | 48.4171 | 48.4171 | 0.0000 |
| 平均交易毛利 點 | -1.7542 | -1.7542 | 0.0000 |
| 最差單筆 點 | -276.0000 | -276.0000 | 0.0000 |
| 最差單筆 / 初始模型權益 % | -0.6900 | -0.6900 | 0.0000 |
| 累計毛利 點（模型成本為零） | -935.0000 | -935.0000 | 0.0000 |
| 已平倉權益 MDD % | -4.9575 | -4.9575 | 0.0000 |
| 日報酬年化 Sharpe（252） | -4.6920 | -4.6920 | 0.0000 |
| 參考線可用率 %（確認率） | 100.0000 | 72.2429 | -27.7571 |
| Vol HIGH/EXTREME 偵測率 %（已校準時段） | 98.6278 | 98.6278 | 0 |
| OI 強度均值（假設常數） | 1.5000 | 1.5000 | 0 |

## 成對分析與覆蓋

Baseline 勝 0；Adaptive 勝 0；平手 537。
有效參考線改變 579 次；方向改變 0 次。
固定 window_secs=300、bar_secs=300，因此 TWAP=當前 close，且 alpha>0；adaptive=alpha×VWAP+(1-alpha)×close，故 close 與參考線的方向一致。此模型下績效相同屬結構性結果，不能推論其他進出場規則的改善。

| 台北日曆日期 | 成對決策數 |
|---|---:|
| 2026-07-06 | 149 |
| 2026-07-07 | 183 |
| 2026-07-08 | 211 |
| 2026-07-09 | 216 |
| 2026-07-10 | 48 |

結果分類：`{"ADAPTIVE_INPUT_UNAVAILABLE": 224, "EVALUATED": 537, "NEXT_PAIRED_BAR_MISSING": 46}`。
Vol 已校準 583 組；未知 224 組，不以正常值補齊。

## 固定模型與限制

- 當前 close 高於／低於參考線做多／做空；等值 flat；下一根連續 5 分 K 的 open 進場、同根 close 出場，固定一單位。
- 只使用決策時已收盤資料計算訊號；未來 K 線僅供成交與結果計算。需要下一根 near/far 均完整才計分，兩組共用樣本。
- OI intensity=1.5、direction=0.65 是假設；quality 是 VolAdjuster 高斯成交量代理，並非歷史 OI 或詐欺品質觀測。
- 零手續費／滑價；所有 PnL 是指數點，非台幣。初始模型權益 40000 點只供百分比與 Sharpe 正規化，不代表實際資金。
- VWAP 使用快照內當期 15:00 重置後可用的完整 NEAR K 線，未保證涵蓋完整交易時段。
- 來源僅涵蓋 7/6 08:46～7/10 05:00，7/10 日盤缺漏；五個日曆日期不等於五個完整交易日。來源無合約代碼與交易所驗真證明。
- MDD 只計已平倉權益，不含持倉內風險。Sharpe 僅五個觀察日期，樣本過短且非獨立，不能視為長期風險估計。
- 此次未調參；未做交易成本、跨時期與樣本外驗證；維持 NO_GO。

## 可重現證據

來源基準 commit：`6390abe13a25f4964a17c03bac503a6e771197b1`。
執行模型、參數、原始資料檢查及 SHA256：`logs/ab_test_20261016/model_manifest.json`。
逐筆決策追蹤：`logs/ab_test_20261016/replay_trace.jsonl`；原觀察模式結果保留於 `observed/`。
比較前已檢查 manifest 所有輸出雜湊、來源雜湊與 exact pair ID／時間戳；重複或未配對記錄會直接失敗。

原始快照檢查：

```json
{
  "data/tmf_near_20260712.csv": {
    "unique_minutes": 4561,
    "identical_duplicates": 0,
    "first_minute": "2026-07-06 08:46:00",
    "last_minute": "2026-07-10 05:00:00"
  },
  "data/tmf_far_20260712.csv": {
    "unique_minutes": 4418,
    "identical_duplicates": 0,
    "first_minute": "2026-07-06 08:46:00",
    "last_minute": "2026-07-10 05:00:00"
  }
}
```

來源及執行程式 SHA256：

```json
{
  "data/tmf_near_20260712.csv": "c9664e17fc963ec18ca21918a95fe22003ea27c963252b6e04266f89c4a84a69",
  "data/tmf_far_20260712.csv": "bcf8dcc88bdeccf7c1fa96e7774ecee3178e856a55e5e61a7fe353b458190e76",
  "/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015/config/adaptive_vwap_config.yaml": "3fe093cac148f6102e8763edc4519f81959fed408687d3969ce419e420c30f6a",
  "/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015/scripts/research/run_adaptive_ab_backtest.py": "b26fe875c79c9b41f615d73a9b5b60ac68337928368957300bab19c63e9f002f",
  "/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015/strategies/plugins/futures/active/mts_adaptive_vwap.py": "383947f6e1e948847fe9fb515c929afb1298235386baf841c10fb1c8cf28c033",
  "/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015/strategies/plugins/futures/active/mts_vol_adjuster.py": "7a9b022b503780b79779c76893ef9ce33ece22a3b1ff049419e353328d3e6f56",
  "/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015/strategies/plugins/futures/active/mts_hvwap_candidate.py": "f525d95499214e171a65ccc758f255a49f4e4e7039309b1da0ae627f5b0a1cf7",
  "/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015/scripts/research/replay_adaptive_vwap.py": "0e01ddcc7810116f45f51e4e07562b2d209c74a3b64f57bc36da0dbcb0a9fe76"
}
```

## 重現指令

先進入本研究 worktree，以全新 output-dir 重跑，避免混入既有結果：

```bash
taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3 scripts/research/run_adaptive_ab_backtest.py --near data/tmf_near_20260712.csv --far data/tmf_far_20260712.csv --start-date 2026-07-06 --end-date 2026-07-10 --output-dir logs/ab_test_reproduction --initial-equity-points 40000 --config config/adaptive_vwap_config.yaml
taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3 scripts/research/compare_ab_results.py --baseline 'logs/ab_test_reproduction/baseline_*.jsonl' --adaptive 'logs/ab_test_reproduction/adaptive_*.jsonl' --output reports/ab_comparison_reproduction.md
```
