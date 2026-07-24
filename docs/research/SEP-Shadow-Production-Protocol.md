# SEP Shadow Production Observation Window Protocol (2026-07-23 ~ 2026-08-06)

## Executive Summary

Strategy Evaluation Platform (SEP，策略評估平台) 即日起正式開啟 **Shadow Production Observation Window (14 天無人值守觀察期)**。

本階段之核心目標為從「機制驗證 (Mechanism Readiness)」跨越至「**實務服務合規 (Observed Service-Level Compliance)**」，累積真實交易 Episode 數據，並為日後策略優化與升級奠定絕對不可竄改之實證基礎。

---

## 一、觀察期與 Change Freeze 凍結協定

### 1. 時間範圍與狀態
* **起算日期 (Start Date)**：`2026-07-23`
* **結束日期 (End Date)**：`2026-08-06` (共計 14 個日曆日)
* **平台狀態 (Platform State)**：`SHADOW_PRODUCTION_ACTIVE`
* **變更凍結狀態 (Change Freeze)**：`ACTIVE`

### 2. Change Freeze 嚴格限制
在 2026-08-06 之前，**嚴禁** 進行以下變更：
- ❌ 嚴禁新增 ResearchOps 功能或擴充架構。
- ❌ 嚴禁修改狀態機語義、Dataset Contract 或 Notification 分級。
- ❌ 嚴禁啟動 R-005 多維參數掃描或修改 Replay Policy 語義。

> **允許變更**：僅限 P0/P1 營運缺陷、權限失效、Cron 中斷、Email 失敗或數據遺失等修復，修復必須建立 `SEP-SHADOW-I-xxx` Shadow Incident 檔案記錄。

---

## 二、SLO 初始校準矩陣 (Service-Level Compliance Matrix)

區分功能性驗證狀態與 14 天運作觀察目標：

| 服務指標 (SLO Metric) | Shadow 起始狀態 (2026-07-23) | 14 天觀察期目標 |
| :--- | :--- | :---: |
| **READY Bundle 註冊延遲** | `ARMED / awaiting observations` | $\le 2$ 小時 |
| **Dataset Hash Validation** | `FUNCTIONALLY VERIFIED` | **100%** |
| **Daily Report 準時率** | `ARMED / insufficient windows` | 截止時間 08:00 ($\ge 99\%$) |
| **Weekly Report 準時率** | `ARMED / insufficient windows` | 截止時間 Sun 12:00 ($\ge 95\%$) |
| **Notification P0 延遲** | `END-TO-END VERIFIED` | $\le 30$ 分鐘 |
| **Pending Notification Age** | `FUNCTIONALLY VERIFIED` | $\le 1$ 小時 |
| **重複 Daily Email 筆數** | `FUNCTIONALLY VERIFIED` | **0** |
| **未解 Quarantine Bundle** | `CURRENTLY 0` | **0** |
| **Replay Fidelity** | `BASELINE PASS` | **100%** |
| **Broker Access on Air4** | `SECURITY GATE VERIFIED` | **0 次成功** |

---

## 三、每日不可變計分卡 (Daily Shadow Scorecard Protocol)

每日透過 `python3 -m sep.cli daily-review` 或 `generate-scorecard` 自動於 [`reports/sep/shadow-production/daily_scorecards/`](file:///Users/mylin/Documents/mylin102/tw-trading-unified/reports/sep/shadow-production/daily_scorecards/) 產出不可變 JSON 記錄，內容包含：
1. `ingestion` (預期/完成次數、最新 Bundle 延遲)
2. `reports` (Daily 產生時間、是否準時、重複筆數)
3. `notifications` (Pending, Sent, Failed, 最長年齡)
4. `datasets` (Hash 驗證率, 未解 Quarantine)
5. `security` (Air4 券商連線成功次數 = 0, Mini 研究執行成功次數 = 0)
6. `replay` (保真度率 = 1.0)

---

## 四、Shadow Exit Review Gate (2026-08-06)

於 2026-08-06 結束時，將進行正式審查，產出 `exit_decision.json`：

```text
GRADUATE    (通過審查，正式升格為 ResearchOps 生産平台)
EXTEND      (基本正常，但 Weekly 樣本或觀察窗口不足，延長觀察)
REMEDIATE   (存在數據完整性、權限隔離或可重現性缺陷，進行修復)
```
