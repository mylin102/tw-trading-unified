# A/B 驗證框架 v2 交付與未通過項目

2026-10-16 Hermes Agent。派工：`/tmp/codex-task-improve-validation.md`。

研究功能已實裝並可重現；**派工的嚴格差異驗收尚未全部通過，策略提升判定 NO_GO**。

## 交付範圍

| 工作 | 狀態與證據 |
|---|---|
| 歷史記憶參考線 | 選方案 B：5 根完整5分K、1500秒有限視窗 EMA，權重1/3；原 TWAP API 保留。60秒方案仍退化，未採用。 |
| 距離／確認規則 | 兩組均有0.1% price-reference 距離、雙腿同向、2根確認及既有 Renko／ADL仲裁；adaptive 額外有0.1% reference shift與 alpha≥0.3。 |
| 成交量診斷 | near/far與時段分別計算；來源單位仍未驗真。採 prior-only rolling30～120根，不以整週統計回填歷史訊號；production參數未改。 |
| 雙腿生命週期 | 直接沿用 `evaluate_lifecycle_actions` 與 entry-side mapping，含 Policy J、Trend Release、停損／逾時／反轉／追蹤；兩組各維護持倉和成交帳。 |
| 差異觀察 | 同狀態候選52/807=6.4436%；實際持倉action21/807=2.6022%。27組共同已平倉中7組出場不同。 |
| 嚴格驗收 | 實際持倉action未達5%，故 `framework_acceptance=NOT_MET`；沒有事後降低門檻或增加資金以製造通過。 |

派工示意的 `price_next_open > price_current_close` 並非 v1 實際訊號規則；v1是決策close相對VWAP的方向。v2同樣只用已完成資料產生訊號，下一根open僅用於成交。

交付報告：[A/B v2](../../reports/ab_comparison_20261016_v2.md)、[成交量診斷](../../reports/volume_calibration_diagnosis.md)、[更新指南](../ADAPTIVE_VWAP_INTEGRATION_GUIDE.md)。

## 最終數值及含義

- 固定20260712快照，807組配對，其中577組雙腿adaptive參考線均有效；可用性造成的3次候選差異未計入5%門檻。
- 27組不重疊、共享進場的假設價差：baseline成本後淨利−7588.9584 TWD，adaptive−8069.0060 TWD，adaptive少480.0476 TWD。
- 相同進場的已平倉比較：baseline勝5、adaptive勝2、平手20。差異存在，沒有績效改善證據。
- EXTREME比例：near6.6092%、far6.9869%；HIGH+EXTREME分別15.6609%、13.2460%。原98.63%指HIGH+EXTREME合計，非單獨EXTREME。
- 兩組同為SPREAD且參考線有效僅199點。全807點另有暖機、冷卻、空倉與169次共同進場保證金不足。這會稀釋實際action差異，但沒有藉此更換驗收分母。
- 100000 TWD是離線假設庫存場景資金，來源產品設定每腿保證金46000；不是paper runtime資金限制的變更或實盤授權。

## 驗證與可重現性

改動前相關基線：145 passed、1 failed（`test_counter_trend_leg_mapping`）。擴展後：**178 passed、相同1項既有失敗**；其中純新增／相容測試127項全部通過。

完整套件在改動前／後各嘗試一次，均為30 passed、1 failed，在既有 `test_unified_config_session_override_merging` 停止；沒有宣稱完整套件通過，也未擴大修復這兩項既有問題。

```bash
taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3 -m pytest tests/test_mts_validation_v2.py tests/test_mts_ab_backtest.py tests/test_mts_adaptive_vwap.py tests/test_mts_adaptive_ab.py tests/test_mts_adaptive_vwap_integration.py tests/strategies/test_mts_lifecycle_adapter.py tests/strategies/test_mts_trend_signal_adapter.py tests/strategies/test_mts_trend_wiring.py tests/strategies/test_policy_j_exit_ownership.py tests/strategies/test_combined_upl_trail_policy.py -q
```

測試包含EMA記憶／暖機／缺根、未來資料不改變先前決策、校準先評分再加入當根、雙腿獨立校準、距離／alpha／shift門檻、entry-side映射、Policy J與硬停損優先、單腿轉移、缺根不延遲成交、不重複出場、雙腿費稅滑價、配對／重複／校準前視／來源雜湊拒絕。

獨立核算結果PASS：

1. 從來源分鐘CSV自行重組完整5分K，逐筆核對VWAP、EMA、prior-only volume mean/std/Z與時間戳。
2. 從逐筆fill自行重建兩組持倉、現金、清算權益，核對27組episode淨利、phase與所有成本。
3. 全新目錄重跑，每日JSONL、portfolio、volume diagnosis及trace等manifest輸出逐位元相同；參數與來源雜湊一致。Git dirty-status描述僅是執行時元資料，不作成交結果相等判定。

純EMA fusion：1000次、每次320根輸入，mean0.602ms、p99 0.907ms、max1.040ms；不含生命週期、校準、IO或upstream OI效能。

## 證據位置與版本

最終權威資料：`logs/ab_test_20261016_v2_verified/`。

- 每日 `baseline_20260706~10.jsonl`、adaptive對應檔、`portfolio.json`、`replay_trace.jsonl`。
- `model_manifest.json`：參數、基準commit與dirty status、來源／輸出SHA256及所有假設。
- `volume_diagnosis.json`、`independent_verification.json`、`verify_independent.py`、`performance.json`。
- `run.log`、`comparison.log`、`tests.log`、`focused_baseline.log`、`full_baseline.log`、`full_after.log`。
- 再現性資料：`logs/ab_test_20261016_v2_verified_reproduction/`。

早期 `v2/` 是中間版本，曾允許看空時釋放NEAR；核對既有entry-side映射後已修正。`v2_final/` 保留修正後、增加entry-status觀測之前的結果。這兩者均不作最終交付；變更未修改距離、alpha、EMA、校準或資金參數。

研究程式main均以macOS background policy啟動；內部輔助`taskpolicy -b -p`遭沙箱拒絕，但外層background啟動成功。此政策會傳遞至子程序。

## 尚未解決的限制

- source只有固定快照的部分7/6～7/10，非完整7月或五個完整交易日；7/10日盤缺漏。
- 無來源字典、合約代碼、真實OI、詐欺品質事件、tick MID或BBO freshness；bar Renko／參考線不能聲稱真實micro-VWAP重建。
- 共用假設進場不等於實際MTS進場紀錄；兩組等待共同flat才重新進場，會耦合後續機會。此處測量exit條件，未宣稱完整entry策略績效。
- Policy J原adapter固定10 TWD/點與92 TWD friction；成交帳按product實際設定計費。原公式未改。closed-bar release-intercept保持拒絕，peak只有bar close代理，非tick確認。
- A/B同時改EMA及門檻，差異不能單獨歸因某項改動；同一週資料已看過，沒有樣本外結論。
- 全807點實際action差異2.60%，未達5%；Adaptive成本後績效亦較差。不得用這份報告放行2026-10-20實盤測試。

## Host與修改證據

Target host Air4；`.deployment-target`為`air4 / offline_research / mts-oi-vol-integration-20261015`；hostname `MacBook-Air-2.local`，ComputerName `MacBook Air (4)`，arm64。

Repository `/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015`，branch `feat/mts-oi-vol-integration-20261015`；改動前commit `3ec716584c994cb82c8925c197001705286b72a1`、工作樹乾淨。研究ticker由futures配置取得TMF；執行模式為offline research，不連broker。

修改production code/config：`mts_adaptive_vwap.py`、`adaptive_vwap_config.yaml`、新增`adaptive_validation_config.yaml`、研究runner/comparator及新增`adaptive_validation_v2.py`。Supporting changes為兩份測試、指南、本紀錄與比較／診斷報告。

未修改cron、PM2、broker、Mini或runtime設定；無推送、無部署。工作狀態只在Air4提交。最終commit、工作樹與各檔雜湊另存本機`change_evidence.json`；原始JSONL與執行日誌不提交Git。

提交準備期間另出現未追蹤`CHANGELOG_PHASE1.md`，非本次產生；保留原檔且排除於提交，不宣稱整個工作樹乾淨。
