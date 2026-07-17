# Baseline Trajectory Reproduction Certification v1 (基線軌跡重現性認證契約)

---

## 1. Certification Requirements (認證合格標準)
若要宣告一個重播引擎與策略代碼版本通過基線重播認證，其重現指標必須符合以下硬性限制：

* **FSM 移轉序列匹配率 (Transition Sequence Match)**：`100.0%`
* **委託意圖匹配率 (Order Intent Match)**：`100.0%`
* **持倉狀態序列匹配率 (Position State Match)**：`100.0%`
* **狀態機轉移原因匹配率 (Reason Match)**：`100.0%`
* **成交價格對齊率 (Price Match Rate)**：`100.0%` (基線重播之虛擬成交價與歷史成交必須分毫不差)
* **時間容許通過率 (Timing Tolerance Pass Rate)**：`>= 98.0%` (容許 2% 的物理排程微小延遲誤差，但差值必須在 $\pm 500\text{ ms}$ 內)

---

## 2. Failure Detection & First Divergence Report (失效判定與首個分歧回報)
當任何一項比對不符時，認證程序必須立刻中斷（Fail-fast），並輸出首個分歧事件（First Divergence Event）的詳細上下文，格式規範如下：

```json
{
  "certified": false,
  "failure_category": "STATE_MISMATCH",
  "first_divergence": {
    "event_time_ns": 1784268150000000000,
    "event_time_utc": "2026-07-17T09:15:50.000000Z",
    "expected_state": "ACTIVE",
    "actual_state": "ARMED",
    "divergent_payload": {
      "strategy_name": "TMF_Calendar_Spread",
      "historical_action": "RELEASE",
      "replayed_action": "NONE",
      "reason_diff": {
        "historical": "Z-score release triggered",
        "replayed": "No action triggered by FSM"
      }
    }
  }
}
```

---

## 3. Data Flow Lock (數據流鎖定機制)
* 系統設計要求：在執行參數掃描或生成反事實報告前，`counterfactual_service` 必須動態執行 `verify_baseline_trajectory()`。
* 若驗證回傳 `certified == false`，將拋出 `ReplayCertificationError`，並阻斷任何進一步的研究輸出與 UI 顯示。此為硬性安全閥。
