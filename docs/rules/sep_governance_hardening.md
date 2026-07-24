# Rule: SEP Governance & Operational Hardening Specification

## 1. Executive Purpose

本規範定義 **Strategy Evaluation Platform (SEP，策略評估平台)** 的 4 大營運強化機制與 10 大 SLO 服務指標，達成資料、權限、程式執行與 Git Promotion 四個層面的絕對隔離。

---

## 2. 4 大營運強化機制 (Operational Hardening)

### 1. 執行角色強行隔離門檻 (Deployment-Role Fail-Closed Gates)
- **.deployment-target 配置**：Air4 正式定名為 `deployment_id: air4`, `host_role: offline_research`。
- **`core/deployment_role_gate.py`** 提供兩大強行阻斷檢查：
  - `assert_research_allowed()`：Mini (Production) 上嘗試執行 SEP / Replay / DOE 立即拋出 `ResearchNotAllowedOnProductionError` 拋錯退出。
  - `assert_broker_access_allowed()`：Air4 (Offline Research) 上嘗試初始化券商 Session / 連線下單立即拋出 `BrokerAccessNotAllowedOnResearchHostError` 拋錯退出。

### 2. 原子化 Dataset Bundle 與 `READY` Marker 協定
- Mini (Production) 的資料導出採 **Staging + Atomic Rename** 流程：
  `寫入暫存目錄` $\to$ `fsync()` $\to$ `計算 SHA-256` $\to$ `寫入 dataset_manifest.json` $\to$ `Atomic Rename 至 research_ready/<build_id>/` $\to$ **`建立 READY 標記檔`**。
- Air4 僅允許拉取含有 `READY` Marker 之 Bundle，禁止直接讀取寫入中之活躍目錄。

### 3. Air4 Pull-Only Cron 與無 `--delete` 增量同步 (時間錯開 de-conflict)
- Air4 主動透過 SSH 讀取 Mini 的 `research_ready/` 下載至 `data/inbox/.staging/`。
- 排程時間與 Notification Dispatcher 完全錯開（Ingest 於 15 分，Dispatcher 於 7, 22, 37, 52 分）。
- `rsync` 保持 **Append-Only** 原則，**嚴禁使用 `--delete`** 參數。

### 4. Inbox Registry 狀態機與 Quarantine 隔離機制
- **6 狀態轉移**：
  `DISCOVERED` $\to$ `TRANSFERRED` $\to$ `HASH_VERIFIED` $\to$ `CONTRACT_VALIDATED` $\to$ `REGISTERED` $\to$ `AVAILABLE_FOR_RESEARCH`
- **Quarantine 隔離**：
  若發生 `READY 標記缺失`、`SHA-256 雜湊不符`、`Manifest 異常` 或 `Build ID 雜湊衝突`，直接轉移至 `QUARANTINED` 狀態存檔於 `data/quarantine/` 並發出告警。

---

## 3. 正式運作 Crontab 規範 (Air4)

```cron
# BEGIN SEP RESEARCHOPS

# Hourly dataset continuous ingestion (15 * * * *)
15 * * * * cd /Users/mylin/Documents/mylin102/tw-trading-unified && /usr/bin/taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified/venv/bin/python3 -m sep.cli ingest >> /Users/mylin/Documents/mylin102/tw-trading-unified/logs/sep_ingest.log 2>&1

# Notification outbox dispatcher (7, 22, 37, 52 * * * * - 避開 15 分碰撞)
7,22,37,52 * * * * cd /Users/mylin/Documents/mylin102/tw-trading-unified && /usr/bin/taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified/venv/bin/python3 -m sep.cli dispatch-notifications >> /Users/mylin/Documents/mylin102/tw-trading-unified/logs/sep_notification.log 2>&1

# Daily operational brief (Monday through Saturday 07:30)
30 7 * * 1-6 cd /Users/mylin/Documents/mylin102/tw-trading-unified && /usr/bin/taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified/venv/bin/python3 -m sep.cli daily-review --send-email >> /Users/mylin/Documents/mylin102/tw-trading-unified/logs/sep_daily_review.log 2>&1

# Weekly statistical research report (Sunday 09:00)
0 9 * * 0 cd /Users/mylin/Documents/mylin102/tw-trading-unified && /Users/mylin/Documents/mylin102/tw-trading-unified/venv/bin/python3 -m sep.cli weekly-research --send-email >> /Users/mylin/Documents/mylin102/tw-trading-unified/logs/sep_weekly_research.log 2>&1

# END SEP RESEARCHOPS
```

---

## 4. ResearchOps 服務品質指標 (SLO Matrix)

| 服務指標 (SLO Metric) | 建議門檻 (Target) | 控制機制與說明 |
| :--- | :---: | :--- |
| **READY Bundle 註冊延遲** | $\le 2$ 小時 | Ingest 於每小時 15 分自動檢視拉取 |
| **Dataset Hash Validation** | **100%** | Ingestion 狀態機 HASH_VERIFIED 檢驗 |
| **Daily Report 準時率** | $\ge 99\%$ | 每日 07:30 自動產出與 catch-up 支援 |
| **Weekly Report 準時率** | $\ge 95\%$ | 週日 09:00 自動產出與 Bootstrap $B=10,000$ |
| **Notification P0 延遲** | $\le 30$ 分鐘 | Outbox Dispatcher 每 15 分鐘派送 |
| **Pending Notification 最長年齡** | $\le 1$ 小時 | 超過時標註 RETRYABLE / PERMANENT_FAILED |
| **重複 Daily Email 筆數** | **0** | 基於 Period + Date 的冪等性過濾 |
| **未解 Quarantine Bundle** | **0** | P0 告警通知人工審查 |
| **Replay Fidelity** | **100%** | 5 大離場指標通過 `test_replay_fidelity.py` |
| **Broker Access on Air4** | **0 次成功** | `assert_broker_access_allowed()` 門檻拋錯阻斷 |

---

## 5. 兩週 Shadow Production 觀察規範

在 Shadow Operations 觀察期間（2 週），**不進行任何預防性架構擴充與參數調整**，專注於平台穩定度驗證：

1. **每日確認**：
   - 最新 READY Bundle 是否按期註冊。
   - Quarantine 數量與重複 Build ID 是否為 0。
   - Daily Report 是否準時發送且無重複信件。
2. **每週確認**：
   - Weekly Report period 與 Evidence Level (E2) 是否穩定。
   - Bootstrap 是否固定使用 Seed = 42。
   - R-005 Trigger 是否僅做 Audit 而未自動開啟搜尋。
