# Rule: Mini (Production) vs Air4 (Research SEP) Architecture & Separation

## 1. Executive Purpose

為保障實盤交易系統之最高穩定性與資源獨佔性，交易執行系統與策略研究平台採**實體與責任完全隔離 (Strict Physical & Role Isolation)**：

```text
                  Mini (Production)
        ┌─────────────────────────────┐
        │ Live Trading                │
        │ Strategy Engine             │
        │ Order Manager               │
        │ Trade Dataset Export        │
        └────────────┬────────────────┘
                     │
              (每日單向同步，唯讀)
                     ▼
        ┌─────────────────────────────┐
        │ Air4 (Research SEP)         │
        │ Research Inbox & Manifest   │
        │ Replay Engine               │
        │ DOE & Bayesian Opt          │
        │ Statistical Inference       │
        │ Reports & SEP Platform      │
        └────────────┬────────────────┘
                     │
          經驗證的新 Policy
                     ▼
          Pull Request → Review → Merge
                     ▼
                 Mini Production
```

---

## 2. 責任矩陣 (Responsibility Matrix)

| 系統功能 / 模組 | Mini (Production) | Air4 (Research / SEP) |
| :--- | :---: | :---: |
| **Live / Paper Trading 實盤下單** | **✅ 獨佔** | ❌ 嚴禁 |
| **Shioaji Broker API 連線** | **✅ 獨佔** | ❌ 嚴禁 |
| **Strategy Execution 策略即時運算** | **✅ 獨佔** | ❌ 嚴禁 |
| **Trade Dataset CSV/Parquet Export** | **✅ 獨佔** | ❌ 嚴禁 |
| **Research Inbox & SHA-256 檢驗** | ❌ 嚴禁 | **✅ 獨佔** |
| **Replay Engine (反事實點位重播)** | ❌ 嚴禁 | **✅ 獨佔** |
| **DOE / Bayesian Optimization 搜尋** | ❌ 嚴禁 | **✅ 獨佔** |
| **Statistical Inference & CI 計算** | ❌ 嚴禁 | **✅ 獨佔** |
| **Research Manifest & Reports 生成** | ❌ 嚴禁 | **✅ 獨佔** |

---

## 3. 單向資料同步與 Inbox 註冊協定 (Unidirectional Sync Protocol)

1. **單向拉取 (Pull-Only)**：
   資料同步僅允許 `Mini ──► Air4` 單向傳輸（經由 `rsync` / `scp` 至 `data/inbox/`）。嚴禁由 Air4 將重播、DOE 或實驗結果反向寫回 Mini。
2. **Mini 保持不可變紀錄 (Immutable Historical Record)**：
   Mini 上的歷史日誌與數據檔為最高神聖不可變資產。
3. **Research Inbox 驗證關卡**：
   所有進入 Air4 的數據必須先進入 `data/inbox/` 經過 `core/research_inbox.py` 驗證：
   - SHA-256 檔案雜湊一致性
   - Manifest 完整性與 Git Commit 比對
   - 驗證通過後正式註冊存檔至 `data/datasets/<build_id>/`

---

## 4. Git 分支與部署治理策略 (Single Trunk & Detached HEAD Governance)

```text
Single Trunk:
master (Air4 研究持續推進 HEAD)

Production Host (Mini):
Detached HEAD pinned at approved production commit SHA
(git switch --detach <APPROVED_PRODUCTION_COMMIT_SHA>)
```

- **單一主幹與本機隔離**：不需維護長期 `research/sep` 分支。Air4 在 `master` 推進研究；Mini 使用 `git switch --detach <APPROVED_PRODUCTION_COMMIT_SHA>` 鎖定於經驗證之 Commit。
- **禁止自動更新**：Mini 嚴禁設定自動 `git pull`。`git fetch` 可常態執行，但 `PM2 restart` 必須發生於 Commit SHA 驗證與 Preflight 測試之後。
- **部署版本紀錄**：部署時需寫入 `.runtime/production_deployment.json` 留存驗證證據（包含 `deployed_commit`, `deployed_at`, `approved_by`, `deployment_mode`, `deployment_reason`）。

---

## 5. Promotion Gate 門檻 (Research-to-Production Governance)

研究成果（Policy / Strategy）進入 Production 候選前，必須同時通過 [core/promotion_gate.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/core/promotion_gate.py) 的完整評估，包含：

1. **Replay validity** (反事實點位重播重建度 100%)
2. **Confirmation-set improvement** (確認集性能改善)
3. **Confidence interval requirement** (信賴區間要求)
4. **Drawdown constraint** (最大回撤限制)
5. **Catastrophic-loss constraint** (極端單筆虧損限制)
6. **Plateau stability** (參數高原穩定度)
7. **Regression suite** (迴歸測試全數通過)
8. **Minimum evidence level** (最低證據等級要求，如 E2+ Counterfactual Replay)

> **註**：統計顯著性（$p < 0.05$）僅為證據鏈之一，不構成單獨 Promotion 的充分條件。

