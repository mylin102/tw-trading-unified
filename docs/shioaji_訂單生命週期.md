# Shioaji 實戰：訂單生命週期注意事項

> 適用場景：使用 Shioaji 進行台股 / 台指期 / 選擇權自動交易，並希望把「委託看不到、狀態不同步、成交回報漏接、誤判可改可刪」這類 execution 問題先處理好。

---

## 1. 核心結論

在 Shioaji 實戰中，**`place_order()` 回傳的 `Trade` 物件不能直接當成最終真相**。  
官方文件明確指出，委託送出後若要取得最新狀態，必須再呼叫 `update_status()`；而在 non-blocking mode (`timeout=0`) 下，初始 `Trade` 還可能缺少 `id`、`seqno`、`status_code`、`order_datetime`、`deals`，狀態也會先顯示為 `Inactive`。因此，正式系統必須把 **下單回傳、order/deal callback、`update_status()` 輪詢校正** 三者一起設計。 :contentReference[oaicite:0]{index=0}

---

## 2. Shioaji 訂單生命週期的正確觀念

### 2.1 不要把「送單成功呼叫 API」誤認為「交易所已受理」

`place_order()` 的成功回傳，只代表 API 呼叫成功返回了一個 `Trade` 物件；真正的委託狀態仍要靠後續回報與 `update_status()` 更新。官方在股票與期貨下單教學都明示：**要更新 `trade` 狀態，需要呼叫 `update_status()`**。 :contentReference[oaicite:1]{index=1}

### 2.2 委託事件與成交事件是兩條不同訊息流

官方文件說明，當你 `place_order`、`update_order`、`cancel_order` 時，會收到 **Order Event**；真正成交時，會收到 **Deal Event**。  
也就是說，系統至少要分清楚三件事：

1. 策略想下單（intent）
2. 委託已送出 / 被交易所接受 / 被修改 / 被取消（order lifecycle）
3. 真正成交與部位變動（deal / fill lifecycle） :contentReference[oaicite:2]{index=2}

### 2.3 callback 很重要，但不能只靠 callback

官方提供 `set_order_callback()` 處理委託 / 成交事件，這對自建交易系統非常有用。  
但官方同時也提供 `update_status()`，而且明講在你無法成功 `update_order` 或 `cancel_order` 時，可以先更新 `trade` 狀態再判斷是否可修改。這代表 **callback 並不是唯一真相來源**；正式系統應該定期做狀態刷新與 reconciliation。 :contentReference[oaicite:3]{index=3}

---

## 3. 官方狀態值，實戰上要怎麼看

Shioaji 文件列出的 `Trade.status.status` 常見值包括：  
- `PendingSubmit`：傳送中
- `PreSubmitted`：預約
- `Submitted`：送出成功
- `Failed`：失敗
- `Cancelled`：已取消
- `Filled`：完全成交
- `PartFilled`：部分成交 :contentReference[oaicite:4]{index=4}

### 3.1 實戰解讀建議

這些狀態不要只當顯示文字，而要映射成你的內部狀態機：

| Shioaji 狀態 | 建議內部語意 |
|---|---|
| `PendingSubmit` | 已建立送單請求，但還不能假設交易所已掛單 |
| `PreSubmitted` | 預約 / 暫存類狀態，未必可視為當前可成交掛單 |
| `Submitted` | 已送達，可進入 working-order 追蹤 |
| `PartFilled` | 必須追蹤剩餘量，不可當作已結束 |
| `Filled` | 此張委託結束，但仍要核對成交明細與部位 |
| `Cancelled` | 委託結束，但要確認取消前是否已有部分成交 |
| `Failed` | 委託失敗，不可假設市場上有掛單 |

> 重點：**`Submitted` 才比較接近「市場上真的有這張單」**；`PendingSubmit` 不能直接當 working order。這個判斷來自官方狀態定義。 :contentReference[oaicite:5]{index=5}

---

## 4. 實戰最常踩的坑

### 4.1 non-blocking mode 會讓你更容易「看不到單」

官方明確說明：當 `timeout=0` 時，`place_order()` 回傳的 `Trade` 可能還沒有 `id`、`seqno`、`status_code`、`order_datetime`、`deals`，狀態先顯示為 `Inactive`。  
所以如果你的系統是高頻批次送單，並使用 non-blocking mode，**不能立刻拿回傳的 `Trade` 當作最終訂單記錄**，而必須等：

1. order event callback  
2. non-blocking place order callback  
3. 或後續 `update_status()` 校正。 :contentReference[oaicite:6]{index=6}

### 4.2 `update_order()` 不是任意修改

官方在股票與期貨教學都寫到：**`update_order` 只能減少數量**。  
因此你的策略引擎不能假設「改單」等同自由編輯委託；很多情況下你需要的是 **cancel + new order**，而不是 update。 :contentReference[oaicite:7]{index=7}

### 4.3 「取消成功」前，先確認該單是否仍可改 / 可刪

官方在 `Update Status` 頁面明講：若你無法成功 `update_order` 或 `cancel_order`，可先對特定 `trade` 做 `update_status()`，再檢查 `OrderStatus` 是否仍可修改。  
實戰上這表示：  
- 不要裸呼叫 cancel 後就假設一定取消成功  
- 先 refresh 狀態，再做刪改判斷 :contentReference[oaicite:8]{index=8}

### 4.4 `subscribe_trade` 若被關掉，你會收不到 order/deal event

官方 login 文件指出，`subscribe_trade` 預設是 `True`，會自動訂閱所有帳號的 Order/Deal Event Callback；若你把它關掉，就不會收到這些回報。  
所以正式環境若要做完整生命週期追蹤，請確認 **沒有誤把 `subscribe_trade=False`**。 :contentReference[oaicite:9]{index=9}

### 4.5 模擬環境可測 API 流程，但不能當成交行為真實替身

官方 simulation mode 支援 `place_order`、`update_order`、`cancel_order`、`update_status`、`list_trades` 等 API。  
這很適合測你的生命週期邏輯，但不代表它能完整重現正式市場的延遲、回報時序、部分成交與異常狀況。 :contentReference[oaicite:10]{index=10}

---

## 5. 正式系統建議採用的訂單真相模型

### 5.1 三層分離

請至少拆成三層：

#### A. Intent
策略想做什麼  
例如：  
- Buy TXF 1 口
- Sell TMF 1 口

#### B. Order
送到 Shioaji / 券商的委託  
例如：  
- `order.id`
- `seqno`
- `ordno`

#### C. Deal / Fill
實際成交  
例如：  
- `trade_id`
- `exchange_seq`
- 成交價
- 成交量

官方 callback 結構本來就把 order event 與 deal event 分開呈現，因此你的內部資料模型也應該分離，而不要用一個欄位混裝所有狀態。 :contentReference[oaicite:11]{index=11}

### 5.2 主鍵不要只靠一個欄位

從官方範例可以看到，委託 / 成交相關欄位可能包含：
- `order.id`
- `seqno`
- `ordno`
- `trade_id`
- `exchange_seq`
- `web_id`
- `custom_field` :contentReference[oaicite:12]{index=12}

因此建議：
- **本地主鍵**：自己產生 `intent_id`
- **Shioaji 委託主鍵**：`order.id`
- **交易所 / 成交追蹤**：`ordno`、`trade_id`、`exchange_seq`
- **策略關聯鍵**：善用 `custom_field`

> 實戰上不要只靠 `ordno` 或只靠 `trade_id`；因為委託與成交是兩層資料。

---

## 6. 建議的內部狀態機

### 6.1 最小可用版本

```text
INTENT_CREATED
-> SUBMITTING
-> PENDING_SUBMIT
-> SUBMITTED
-> PART_FILLED
-> FILLED

INTENT_CREATED
-> SUBMITTING
-> FAILED

SUBMITTED
-> CANCEL_REQUESTED
-> CANCELLED

PART_FILLED
-> CANCEL_REQUESTED
-> CANCELLED_PARTIAL

6.2 與 Shioaji 狀態對應
內部狀態	Shioaji 訊號來源
PENDING_SUBMIT	trade.status.status == PendingSubmit
SUBMITTED	trade.status.status == Submitted
PART_FILLED	trade.status.status == PartFilled 或收到 deal 但未滿量
FILLED	trade.status.status == Filled
FAILED	trade.status.status == Failed
CANCELLED	trade.status.status == Cancelled
注意：實務上 PartFilled 與 deal event 要同時看，因為 order status 與成交回報可能不是完全同步抵達。官方文件把 order event 與 deal event 分開，也側面反映了這點。

7. 正式系統必做：reconciliation
7.1 為什麼一定要做
官方設計同時提供：
place_order
callback
update_status
list_trades
這很明顯表示：
Shioaji 的委託生命週期不是只靠單次 API 回傳就能完成管理。
7.2 建議做法
每隔固定秒數：
針對在途中的 trade 呼叫 update_status(account, trade=trade)
定期 update_status(account) 全帳號刷新
比對本地狀態與 callback 累積事件
檢查：
本地認為有 working order，但刷新後已不存在
本地認為未成交，但 deal 已出現
本地認為可改單，但刷新後其實已成交 / 已取消
local qty / filled qty / remaining qty 不一致
8. callback 設計建議
8.1 一定要保留原始 callback payload
官方 order event callback 結構包含：
operation
order
status
contract
deal event 則包含：
trade_id
exchange_seq
broker_id
account_id
action
code
price
quantity
ts
等欄位。
因此建議：
原始 payload 全量落盤
另做 normalized event table
不要只寫 summary log

8.2 callback 只做輕量處理
因為 callback 是事件入口，建議只做：
parse
append log / queue
更新 in-memory order book
不要在 callback 裡做重運算、回測、重新選股、複雜風控，以免阻塞事件處理。
這點雖然官方沒有直接寫成規範，但從其 callback / non-blocking 設計可合理推論：事件處理應保持輕量，避免干擾交易流程。

9. 實戰守則
9.1 下單後不要立刻只看一次狀態就結論
建議流程：
trade = place_order(...)
記錄本地 intent_id
等 callback
若短時間內未收到完整資料，執行 update_status()
再決定是否：
視為 working
重送
cancel
raise alert
9.2 PendingSubmit 期間禁止重複送同方向單
因為此時委託可能已在路上，但你還沒看到完整狀態。若立刻補送，很容易重複掛單。這在 non-blocking mode 特別危險。
9.3 部分成交一定要追剩餘量
PartFilled 不是結束狀態。
取消、改量、反向單、防重複單，都必須以 remaining qty 為準，而不是原始 quantity。官方狀態列出 PartFilled，且 callback / deal event 皆能提供進一步資訊。
9.4 刪單後一定要再刷新一次
不要假設 cancel_order() 呼叫成功就等於市場上已無該單。
先以 callback 接收，再用 update_status() 校正。官方的 Update Status 頁面就是為了這類情況而設計。
9.5 正式環境請保留 custom_field
官方範例在股票 / 成交回報裡都示範了 custom_field。
建議拿來放：
strategy name
signal id
intent id
run id
這對事後追查「哪個策略、哪次訊號、哪個執行批次」非常有用。
10. 建議監控指標
P0
submit_to_submitted_latency_ms
missing_order_visibility_count
pending_submit_timeout_count
cancel_requested_but_still_working_count
P1
partfill_stuck_count
deal_without_local_order_count
local_order_without_broker_refresh_match_count
P2
callback_gap_seconds
update_status_reconciliation_count
duplicate_submit_prevented_count
這些是實務建議，不是官方既有欄位；但它們正是根據 Shioaji 的 callback + update_status() 雙軌設計推導出的運維重點。
11. 建議的最小實作流程
trade = api.place_order(contract, order)

register_intent(intent_id, strategy_id, signal_id)
register_trade_stub(intent_id, trade)

# 等 callback 一小段時間
# callback 收到後，更新 order/deal state

if not is_trade_visible(intent_id):
    api.update_status(account=api.futopt_account, trade=trade)

state = get_latest_state(intent_id)

if state in ["PendingSubmit", "Inactive"]:
    hold_and_retry_reconcile()

elif state == "Submitted":
    mark_working()

elif state == "PartFilled":
    update_remaining_qty()

elif state == "Filled":
    finalize_order()

elif state in ["Failed", "Cancelled"]:
    close_order()
12. 最後的實戰原則
原則一：Shioaji 訂單生命週期一定要「事件 + 輪詢」雙軌
只靠 callback，不夠穩。
只靠 update_status()，也不夠即時。
兩者要一起用。
原則二：不要把委託狀態與成交狀態混為一談
Order event 與 deal event 是不同資料。
你的資料模型與風控模型也要分開。
原則三：non-blocking mode 一定要加額外保護
timeout=0 時初始 Trade 可能資訊不完整、狀態先是 Inactive，因此不能拿來直接做補單判斷。
原則四：所有刪改單前，先 refresh 狀態
官方已明講：需要時先 update_status() 再判斷能否 update_order / cancel_order。
原則五：正式環境一定要能追溯
至少保留：
intent_id
order.id
seqno
ordno
trade_id
exchange_seq
custom_field
原始 callback payload


