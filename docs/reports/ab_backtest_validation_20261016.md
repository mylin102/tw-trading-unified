# 2026-10-16 A/B 代理回放交付與驗證

作者：Hermes Agent。使用者在結果產生前同意固定 20260712 快照、807 個決策點與固定代理模型。

研究回放驗收通過；策略提升判定維持 **NO_GO**。
比較結果：[ab_comparison_20261016.md](../../reports/ab_comparison_20261016.md)，機器可讀結果位於同名 JSON。

## 執行目標與範圍

| 欄位 | 證據 |
|---|---|
| Target host | Air4，`MacBook-Air-2.local` / `MacBook Air (4)` / arm64 |
| Deployment identity | `.deployment-target`: `air4` / `offline_research` / `mts-oi-vol-integration-20261015` |
| Repository | `/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015` |
| Branch | `feat/mts-oi-vol-integration-20261015` |
| Commit before | `6390abe13a25f4964a17c03bac503a6e771197b1`；開始前工作樹乾淨 |
| Production code changes | 僅新增 `scripts/research/run_adaptive_ab_backtest.py`、`scripts/research/compare_ab_results.py` |
| Supporting changes | `tests/test_mts_ab_backtest.py`、比較報告 MD/JSON、本紀錄 |
| Runtime path | 僅在本研究 worktree 使用 deploy-cli-20261015 的 venv Python；未啟動交易 runtime |
| Cron / PM2 / deployment | 無修改、無重啟、無部署、無推送；Mini 未同步、未驗證 |

新增獨立代理回放 CLI，沿用既有完整 K 線載入、AdaptiveVWAP、VolAdjuster 與權益統計。
原本 `replay_adaptive_vwap.py` 保留觀察模式；未提供 OI／品質事件的原觀察回放資料亦保留，未當成代理交易績效。

## 驗收與統計

| 項目 | 結果 |
|---|---|
| 逐筆記錄 | baseline 與 adaptive 各 807 筆，共 5 個日期；10 個每日 JSONL |
| 共同結果 | 537 組；每組 533 筆交易、4 筆 flat |
| 缺漏 | 224 組未校準輸入、46 組下一根配對 K 線缺漏；PnL 為 null |
| 交易勝率 | 兩組均 48.7805% |
| 平均交易毛利 | 兩組均 −1.7542 指數點 |
| 累計毛利 | 兩組均 −935 指數點，成本假設為零 |
| 最差交易 / MDD | 兩組均 −276 點 / −4.9575% |
| 成對勝負 | baseline 0 勝、adaptive 0 勝、537 平手 |
| 關鍵指標 | 比較報告含 12 列指標，超過派工要求 8 項 |
| 資料一致性 | 無衝突重複分鐘或非法 OHLC；配對 ID、輸入、時間戳、來源與輸出 SHA256 通過 |

window_secs=bar_secs=300，使 TWAP 等於當前 close；alpha>0，因此方向代理下兩組方向相同，結果無改善。
沒有為了製造差異調整規則或參數。

## 驗證結果

改動前 88 項相關測試全通過；改動後包括 19 項新測試，共 **107 passed**。
覆蓋下一根 open 成交、未來價格變動不影響當前訊號、缺漏保留 null、禁止覆寫、錯誤配對／重複拒絕、來源／輸出雜湊拒絕、非校準時段與手算 PnL/MDD。

```bash
taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3 -m pytest tests/test_mts_ab_backtest.py tests/test_mts_adaptive_vwap.py tests/test_mts_adaptive_ab.py tests/test_mts_adaptive_vwap_integration.py -q
```

獨立以 CSV 分鐘資料重組完整 5 分 K 線，重新核算每筆成交、訊號、VWAP、−935 點總和與 MDD，結果 PASS。
相同輸入與規則在全新目錄重跑；10 個每日 JSONL、追蹤 JSONL 與 manifest 均逐位元一致。

所有迴圈以 `taskpolicy -c background` 啟動。程式內輔助 `taskpolicy -b -p` 在沙箱顯示 Operation not permitted；外層 background 政策啟動成功，回放與測試 exit code 均為 0。

## 本機證據與限制

`logs/ab_test_20261016/` 保留：

- `baseline_20260706.jsonl`～`baseline_20260710.jsonl` 與 adaptive 對應檔。
- `model_manifest.json`：固定模型、設定、原始 CSV 檢查、來源基準 commit、程式／輸入／輸出 SHA256。
- `replay_trace.jsonl`、`run.log`、`comparison.log`、`tests.log`。
- `verification.json`、`verify_raw_minutes.py`：獨立分鐘資料核對及再現性證據。
- `data_inventory.json`：最初快照盤點；未混用其他重疊快照。
- `observed/`：原觀察模式回放，無觀測 OI／品質訊號的可用 adaptive 結果為零。

再現性重跑輸出位於 `logs/ab_test_20261016_reproduction/`。
原始 JSONL、執行日誌與快照均不提交 Git；只提交研究程式、測試及分析報告。

來源缺少 7/10 日盤、合約代碼與交易所驗真；OI 是常數假設，quality 是成交量代理。
成本為零、固定一根持有、初始模型權益 40000 **點**皆為研究假設，非實際 MTS 決策與台幣資金。
Vol HIGH/EXTREME 佔已校準樣本 98.6278%，僅反映固定校準模型在本資料上的分類，不代表真實詐欺率。
完整交易成本、實際 OI／品質事件、五個完整交易日與樣本外驗證仍不足；禁止據此提升至正式交易。
