# I1 EVENT-SCHEMA 收斂計畫

日期:2026-11-11  
Gate: `scripts/ci/check_event_schema.py`  
現況:**RED,59 筆**

## 1. 為什麼要做(量化)

事件 log(`mts_spread_events.jsonl`)同時是稽核軌跡與重建來源。dashboard、
replay 工具、事後檢討全部**按事件名稱解析**。同名事件在不同程式路徑下欄位不同時,
消費者不會報錯——它讀到 `None`,把「這個 emitter 根本沒提供」誤讀成「值不存在」。

這與 2026-11-10 修正的 `float(_age or 0.0)` 是同一類缺陷:值看起來是「沒有」,
而不是「錯的」。

量測(2026-11-11,HEAD `0a2b583c`):

| 指標 | 數值 |
|---|---|
| `*_append_event` 呼叫點 | 170 |
| 相異事件名稱 | 99 |
| 有 >1 種欄位形狀的事件 | 11 |
| 違規總數 | 59 |

## 2. 現況明細

### A. NON_LITERAL_EVENT_NAME — 7 筆
事件名來自變數,無法鎖定任何 schema:

| 位置 | 狀況 |
|---|---|
| `core/lifecycle_provenance.py:195` | 名稱由呼叫端傳入 |
| `strategies/futures/monitor.py:3571,4849,5432,8558` | 變數 |
| `strategies/plugins/futures/active/tmf_spread.py:6364` | `_iid_for_ev` |
| `main.py:1815` | 變數 |

### B. UNVERIFIABLE_FIELDS — 25 筆
`**payload` 在 gate 看不到的地方組裝(`**snap`、`**kwargs`、`**self._x`)。

### C. SCHEMA_DRIFT — 27 筆 / 11 事件

| 事件 | 呼叫點 | 形狀數 | 缺欄位重點 |
|---|---|---|---|
| `ORDER_INTENT_BLOCKED` | 16 | 6 | 部分**完全沒有 trade_id** |
| `HVWAP_RELEASE_BLOCKED` | 4 | 4 | 寬度/報價年齡/決策時間各只有部分 |
| `ORDER_REJECTED_LOCAL` | 6 | 3 | 部分無 trade_id |
| `ORDER_SUBMITTED` | 11 | 2 | 部分走 `**` 展開 |
| `PAPER_EXIT_INTENT_RECONCILE_FAILED` | 2 | 2 | 一筆無 intent_id/trade_id |
| `FDEAL_CALLBACK_REJECTED` | 2 | 2 | 一筆無 code |
| `POLICY_J_PEAK_REJECTED` | 3 | 2 | peak 欄位不對齊 |
| `POLICY_J_TRIGGER_SUPPRESSED` | 2 | 2 | 一筆給 entry 價、一筆給 guard 時間 |
| `ENTRY_AUDIT` | 3 | 2 | 策略層與 monitor 層欄位不同 |
| `ENTRY_BLOCKED_STALE_QUOTE` | 2 | 2 | 一筆只有 threshold |
| `LEG_LOCK_PAIR_PARTIAL` | 2 | 2 | 一筆無 contract |

## 3. 修法:在寫入層正規化,不是改 99 個呼叫點

gstack reuse ladder 第 1 條:共用函式加一道防護,勝過每個呼叫端各加一道。

三個 writer 都存在(`core/lifecycle_provenance.py:258`、
`strategies/plugins/futures/active/tmf_spread.py:675`、`core/exit_intent.py:609`)。
在其共用的落地點之前插入 schema 正規化:

```python
# core/event_schema.py
EVENT_FIELDS: dict[str, frozenset[str]] = {
    "ORDER_INTENT_BLOCKED": frozenset({...union...}),
    ...
}

def normalize(event: str, payload: dict) -> dict:
    """宣告過的欄位一律存在;未宣告的欄位是違約,直接拒絕寫入。"""
    declared = EVENT_FIELDS.get(event)
    if declared is None:
        raise EventSchemaError(f"UNDECLARED_EVENT:{event}")
    extra = set(payload) - declared
    if extra:
        raise EventSchemaError(f"UNDECLARED_FIELDS:{event}:{sorted(extra)}")
    return {k: payload.get(k) for k in declared}
```

**為什麼是補 None 而不是丟棄**:消費者可依賴「鍵一定存在」;缺值語意從
「這個 emitter 沒提供」變成明確的 `None`。同時**拒絕未宣告欄位**,
避免新增欄位時悄悄長出第二種形狀。

### 為什麼不用 EVENT_SCHEMA 白名單宣告變體
宣告變體等於承認「同一事件可以有兩種形狀」,那正是本 gate 要消滅的東西。
正規化讓「一種事件 = 一種形狀」在**執行期**成立,不只是靜態檢查通過。

## 4. 執行順序(依風險/相依)

1. **A 類(7 筆,零風險)**:把變數事件名改為字面值。純字串替換。
2. **B 類(25 筆)**:把 `**payload` 改成顯式 kwargs 或靜態字典。
   先做 `_ev_meta` / `_risk_meta` 這類同檔 helper(已可解析),
   再做真正動態的(`**snap`、`**kwargs`)。
3. **C 類前置:建 registry**:由 11 個事件的聯集產生 `EVENT_FIELDS`,
   在 writer 加 `normalize()`,**先不啟用嚴格模式**,只記錄差異,確認一輪無異常。
4. **C 類:啟用嚴格模式**,跑 gate 到 0。
5. **收尾**:gate 併入 `--strict` 全綠、AGENTS.md 記錄、部署。

順序理由:1、2 不改執行期行為,先把「無法檢查」的面積縮小;3 的 registry
必須在 4 之前完成,否則正規化沒有依據。3 與 4 之間需要一輪觀察,
因為它會改變實際寫入的 log 內容——這是唯一有生產風險的一步。

## 5. 驗收條件

1. `python scripts/ci/check_event_schema.py` exit 0,且 `--strict` 同。
2. 每個事件在 `EVENT_FIELDS` 的欄位集合 == 所有呼叫點欄位的聯集(以 gate 比對)。
3. 契約測試新增:未宣告事件 → 拋 `UNDECLARED_EVENT`;未宣告欄位 →
   `UNDECLARED_FIELDS`;缺欄位 → 補 `None` 且鍵存在。
4. 現有 `test_squeeze_fire_scout.py` 21 passed 不回歸。
5. 部署後 24h 內 `UNDECLARED_EVENT` / `UNDECLARED_FIELDS` 出現次數 = 0。

## 6. 不做事項(Out of Scope)

- 不重新命名任何既有事件(消費者依賴名稱)。
- 不刪除任何既有欄位(只補 None)。
- 不改 dashboard 的解析邏輯。
- 不動 `tests/` 下的 fixture 呼叫點。

## 7. 回滾

`EVENT_FIELDS` + `normalize()` 是單一新檔 + writer 內三行呼叫。回滾 = revert
該 commit;log 內容回到「有就寫、沒有就不寫」。gate 保持 RED 但不影響執行期。

## 8. 風險

| 風險 | 緩解 |
|---|---|
| 正規化改變既有 log 形狀,舊 replay 工具解析異常 | 只新增鍵、不改值;先在 PAPER 觀察一輪 |
| `UNDECLARED_EVENT` 誤擋新事件 | 啟動即失敗可見;部署前用 gate 先行驗證 |
| 25 筆 `**payload` 改寫引入語意差異 | 每批改完立即跑 gate + 相關測試 |
