# Live Trading Transition SOP & Gap Register

**Status**: ACTIVE (2026-08-03)
**Scope**: Paper → Live execution transition (order route), MTS futures
**Related**: `core/mode_transition.py`, `core/order_management/order_manager.py`,
`strategies/futures/monitor.py`, `config/futures.yaml`

---

## 1. Current Order Route (paper)

```
signal (tmf_spread / monitor)
  → OrderManager.submit()            core/order_management/order_manager.py:505
      ├─ L1 Gate: assert_live_order_allowed()   core/mode_transition.py:162
      │    (paper: requested_mode != LIVE → pass-through)
      ├─ Paper Drain Gate: assert_entry_allowed()
      └─ Route: mode == "live" → _submit_live (broker_adapter.place_order_object)
                mode == "paper" → _submit_paper (PaperFillSimulator)
```

## 2. Transition States (mode_transition.py)

| state_namespace | requested_mode | effective_mode      | live_order_allowed | 送單 |
|-----------------|----------------|---------------------|--------------------|------|
| paper           | PAPER          | PAPER_ACTIVE        | False              | paper fill |
| live            | LIVE           | LIVE_PREFLIGHT      | False              | **blocked** |
| live            | LIVE           | LIVE_READY          | True               | live |
| live            | LIVE           | LIVE_QUARANTINED    | False              | blocked |

- `paper_context()` → PAPER_ACTIVE（目前）
- `live_preflight_context()` → LIVE_PREFLIGHT（config `live_trading: true` 時）
- `with_effective_mode()` → 唯一可改 state 的途徑（frozen dataclass 的 replace）

## 3. Dry-Run Finding (2026-08-03)

**GAP-1 (CRITICAL)**: `PREFLIGHT → LIVE_READY` transition 未接線。
`with_effective_mode` 只有定義，全 repo 無呼叫點。
→ 即使 `live_trading: true`，所有 live 單仍被 L1 Gate 擋
  （`EFFECTIVE_MODE_NOT_LIVE_READY` → REJECTED）。
→ 目前「實際上無法切換」— fail-closed 永久生效（安全，但最後一哩缺失）。

**GAP-2**: `main.py:852` 的「LIVE_PREFLIGHT 風控與賬戶檢測」只是 UI 訊息，
無實際 account/margin/connection 檢查 code。preflight 是名義上的。

**GAP-3**: `_submit_live` 依賴 `broker_adapter.place_order_object` 的存在性
（hasattr 檢查）；live adapter 的契約未單獨測試。

## 4. Transition Design (authorized flow)

```
觸發: config live_trading: true + 啟動（main.py）
流程:
  1. live_preflight_context() → LIVE_PREFLIGHT（現有）
  2. preflight_validate()（新 — 實質檢查）:
     - broker 已 login（api 非 None + login 狀態）
     - 帳戶查詢成功（account balance 可讀）
     - 保證金足夠（margin 檢查 — 依策略參數）
     - contracts 已載入（near/far contract code 解析成功）
     - 全部通過 → with_effective_mode(LIVE_READY, live_order_allowed=True)
     - 任一失敗 → with_effective_mode(LIVE_QUARANTINED)（fail-closed）
  3. LIVE_READY 後: OrderManager L1 Gate 放行 → _submit_live（真實單）
  4. 失敗路徑: LIVE_QUARANTINED 永久擋單（需人工介入）
```

安全原則:
- paper 模式（live_trading=false）→ transition 不觸發 → 零行為影響
- 所有檢查實質（非 UI 訊息）
- LIVE_QUARANTINED 為永久 fail-closed（人工復原）
- transition 完成後仍受: monitor 層 live_ready（4488）、margin 檢查、
  Paper Drain Gate、送單 tag（_LIVE/_PAPER）多層防護

## 5. Go-Live Preconditions (NOT yet met — 2026-08-02 評估)

1. 假帳標記（guard 前後分隔 — 績效檢討）
2. 觀察期（真實 replay 確認進場品質 — 26/28 trail 虧損問題）
3. 參數驗證（activation/trail_dist 掃描未完成）
4. 成本/滑價納入（fees + slippage）
5. 流動性驗證（spread 進出價品質）

→ 目前**不建議**切換。Transition 實作是「補齊機制」，不是「授權切換」。

## 6. 驗收（transition 實作後）

- [ ] preflight_validate 的 4 項檢查有測試（mock 通過/失敗各）
- [ ] paper 模式行為不變（live_trading=false → 無 transition → 測試全綠）
- [ ] LIVE_QUARANTINED 路徑測試（任一檢查失敗 → 擋單）
- [ ] LIVE_READY 路徑測試（全過 → L1 Gate 放行）
- [ ] dry-run 驗證（不實際切 — 用 mock api 走一遍）
