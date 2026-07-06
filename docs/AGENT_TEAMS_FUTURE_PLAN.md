# Agent Teams 多通道協作架構 — 未來規劃

## 起源

參考 Hermes 製造業 Agent Teams 的跨通道、跨平台架構驗證。

該系統的核心不為單一 chatbot，而是：

# 一個多入口、多 Agent、多通道同步的 trading runtime

---

## 核心觀察

我們的 trading system 本質上已經是多 Agent 協作：

| 現有元件 | 對應角色 |
| --------- | ----------- |
| strategy_router | 主控 Agent — 判斷 regime，決定誰上場 |
| squeeze_fire_scout | 專業 Agent — 偵測 squeeze fire |
| counter_vwap / spring_upthrust | 專業 Agent — 反轉交易 |
| exit_manager (tick+bar) | 專業 Agent — 出場管理 |
| vol_state_machine | 專業 Agent — 波動率狀態 |
| regime classifier | 專業 Agent — 市場狀態分類 |

但目前缺乏：

# 正式派工稽核 × 狀態可視化 × 跨通道同步

---

## 四層架構（未來目標）

```
第一層：使用者入口
  ├── CLI (現有 terminal log)
  ├── Dashboard (現有 Streamlit)
  ├── Discord (現有 notification)
  └── Telegram (現有 notification)

第二層：Gateway / Adapter
  ├── Hermes Agent (現有 AGENTS.md 驅動)
  ├── Discord Bot gateway (現有 shioaji_bot)
  └── Dashboard adapter (Streamlit 讀 audit JSONL)

第三層：Agent Teams
  ├── strategy_router (主控 Agent)
  ├── squeeze_fire_scout (專業 Agent)
  ├── counter_vwap (專業 Agent)
  ├── exit_manager (專業 Agent)
  ├── vol_state_machine (專業 Agent)
  └── regime classifier (專業 Agent)

第四層：派工稽核 × 視覺化
  ├── delegate_with_audit (JSONL + Markdown trail)
  ├── RouterTrace (現有 — 已是每 bar audit)
  └── Dashboard Agent Status (新 — 只讀 audit 投影)
```

---

## 最直接的差距：不變 repo 可加

## 1. Audit Hook — 決策稽核層

現狀：

# RouterTrace 已有 per-bar 決策紀錄

但缺少：

* 跨 Agent 派工的 task id
* 資料來源標註（哪個 bar、哪個 snapshot）
* 風險等級標註
* 「是否只是查詢 vs 有改動資料」的 flag

未來 hook 設計（不影響原有 pipeline）：

```python
def audit_decision(
    task_id: str,
    agent: str,        # "strategy_router" / "exit_manager"
    action: str,       # "ENTRY_LONG" / "STOP_LOSS"
    source: str,       # "bar_20260519_0900"
    risk: str,         # "low" / "medium" / "high"
    data_sources: list,
    is_query_only: bool,
    data_changed: bool,
    data_gaps: list,
):
```

寫入 `logs/audit_trail/audit_YYYYMMDD.jsonl`

# 不碰 pipeline 任何一行 code

---

## 2. Agent Status — Dashboard 投影層

現狀：

# Dashboard 顯示 data feed、positions、PnL

缺少：

* 每個 Agent 的「最近一次決策」卡
* Agent 狀態燈（active / idle / error / no_data）
* 上次決策時間與原因
* 資料來源新鮮度

未來方向（只讀層）：

```text
┌─────────────────────────────────┐
│ Agent Teams Status              │
├─────────────────────────────────┤
│ 🟢 strategy_router   09:30     │
│    regime=TREND  selected=orb  │
│ 🟡 squeeze_scout    09:28     │
│    triggered=NO_FIRE_EVENT     │
│ 🔴 exit_manager     09:15     │
│    reason=ATR_TRAIL bar_layer  │
│ ⚪ vol_state         08:45     │
│    state=high  age=45m stale   │
└─────────────────────────────────┘
```

從 audit JSONL 讀取，不寫入 trading pipeline。

---

## 3. 多通道通知 — 結果同步

現狀：

# Discord / Telegram 已有 exit 通知

但缺少：

* task id 關聯
* 資料來源與時間
* 風險提醒
* 哪些資訊仍然不足

未來通知格式：

```
[ExitManager] task=exit-20260519-0930
reason=ATR_TRAIL (bar layer)
src_bar=20260519_0900_1min
risk=stop_loss_hit, loss=-1.2U
data_quality=ok, vol_state=high
data_gaps=spread_feed_stale(18m)
```

---

## 4. 主控 Agent 升級 — strategy_router v3

現狀：

# strategy_router 是 declarative rule-based

未來方向：

引入類似 manufacturing-main 的「判斷 → 分派 → 彙整 → 裁決」循環：

```text
收到新 bar
  ↓
判斷當前 market regime (regime classifier)
  ↓
分派給符合 regime 的策略們 (現有)
  ↓
彙整各策略的 StrategyEval + edge_score (現有)
  ↓
裁決：選一個或不選 (現有)
  ↓
audit 紀錄 (新)
  ↓
結果同步到通知通道 (新)
```

但：

# 這階段不升級 router

需要等以下前提穩定後才開 branch：

* VolState hysteresis 穩定
* Exit contract audit 完成 (P2)
* Regime classifier 不再卡死
* MTS 交易頻率恢復

---

## 實施時機

| 前提條件 | 狀態 |
| ---------- | ------ |
| VolState 3-strike skew 穩定 | 進行中 |
| Exit contract P2 完成 | 進行中 |
| MTS 交易頻率恢復 | 未完成 |
| Regime classifier 卡死修復 | 未完成 |
| RouterTrace 完整性驗證 | 已通過 |

# 系統穩定後，開新 branch 執行

---

## 實施順序（未來）

```
Phase 1: Audit Hook（最小侵入）
  ├── 關鍵決策點加 audit_record()
  ├── JSONL 寫入 logs/audit_trail/
  └── 不變 pipeline 任何邏輯

Phase 2: Dashboard Agent Status（只讀）
  ├── Streamlit 新 tab 讀 audit JSONL
  └── Agent 狀態燈 + 最近決策卡

Phase 3: 通知強化（metadata 補充）
  ├── task id 加入 Discord/TG 通知
  ├── 資料來源與風險等級
  └── 資訊不足 flag

Phase 4: strategy_router v3（主控升級）
  ├── 判斷 → 分派 → 彙整 → 裁決循環
  ├── 正式 delegate_with_audit
  └── 跨 Agent task id 鏈
```

---

## 關鍵原則

### 1. Audit 在 Pipeline 之後

先有穩定交易，才有稽核價值。

### 2. 可視化只讀不寫

# Dashboard 永遠是投影層

不是控制層。

### 3. 通知是 audit 的副產品

不是反過來 — 先有紀錄，才有通知。

### 4. 主控升級是最後一步

# 不是第一步

等到所有專業 Agent 穩定後，再談 router 升級。

---

## 核心哲學

### 現在：

# 先讓系統能穩定交易

### 之後：

# 再讓系統能證明它做了什麼

當 AI 從「黑盒訊號產生器」變成：

* 有分工（每個 Agent 明確職責）
* 有紀錄（每步決策可追溯）
* 有狀態（Dashboard 看板）
* 有通知（結果同步到溝通工具）

它才真正能落地到交易流程中。

---

## 相關文件

* `docs/architecture/system_overview.md` — 現有架構
* `docs/architecture/strategy_router.md` — 現有 router
* `docs/reduction_plan.md` — MTS 降維計劃
* `AGENTS.md` — 開發規則
