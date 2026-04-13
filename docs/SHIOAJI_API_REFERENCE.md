# Shioaji API 快速參考

> 來源：https://sinotrade.github.io/zh/
> 本文件為開發參考用摘要，完整文件請見官方網站。

---

## 1. 登入

```python
import shioaji as sj

# 正式環境
api = sj.Shioaji()

# 模擬環境（下單不會真實成交，用於測試）
api = sj.Shioaji(simulation=True)

api.login(api_key="YOUR_API_KEY", secret_key="YOUR_SECRET_KEY")
```

### Login 參數

| 參數 | 型別 | 預設 | 說明 |
|------|------|------|------|
| api_key | str | — | API 金鑰 |
| secret_key | str | — | 密鑰 |
| fetch_contract | bool | True | 是否下載商品檔 |
| contracts_timeout | int | 0 ms | 商品檔 timeout |
| contracts_cb | Callable | None | 商品檔下載完成 callback |
| subscribe_trade | bool | True | 是否訂閱委託/成交回報 |
| receive_window | int | 30000 ms | 登入有效執行時間 |

### CA 憑證啟用（下單必須）

```python
api.activate_ca(
    ca_path="/path/to/cert.pfx",
    ca_passwd="YOUR_CA_PASSWORD",
    person_id="YOUR_PERSON_ID",
)
```

### 帳號

```python
api.list_accounts()          # 所有帳號
api.stock_account             # 預設股票帳號
api.futopt_account            # 預設期權帳號
api.set_default_account(acc)  # 切換預設帳號
```

- `signed=True` 才能下單，否則需到券商簽署 API 服務條款
- 模擬模式下 `signed` 自動為 `True`

### 登出

```python
api.logout()  # 務必登出，有連線數限制
```

---

## 2. 模擬模式

```python
api = sj.Shioaji(simulation=True)
```

### 可用 API

| 類別 | API |
|------|-----|
| 行情 | `quote.subscribe`, `quote.unsubscribe`, `ticks`, `kbars`, `snapshots` |
| 下單 | `place_order`, `update_order`, `cancel_order`, `update_status`, `list_trades` |
| 帳務 | `list_positions`, `list_profit_loss` |

---

## 3. 期貨/選擇權下單

### 委託單參數

| 參數 | 型別 | 說明 |
|------|------|------|
| price | float/int | 委託價格 |
| quantity | int | 委託數量 |
| action | Action | `Buy` / `Sell` |
| price_type | FuturesPriceType | `LMT`(限價) / `MKT`(市價) / `MKP`(範圍市價) |
| order_type | OrderType | `ROD` / `IOC` / `FOK` |
| octype | FuturesOCType | `Auto`(自動) / `New`(新倉) / `Cover`(平倉) / `DayTrade`(當沖) |
| account | Account | 下單帳號 |

### 下單

```python
contract = api.Contracts.Futures["TXFR1"]  # 台指期近月

order = api.Order(
    action=sj.constant.Action.Buy,
    price=contract.reference,
    quantity=1,
    price_type=sj.constant.FuturesPriceType.LMT,
    order_type=sj.constant.OrderType.ROD,
    octype=sj.constant.FuturesOCType.Auto,
    account=api.futopt_account,
)

trade = api.place_order(contract, order)
```

### 更新狀態

```python
api.update_status(api.futopt_account)
print(trade.status.status)  # PendingSubmit → Submitted → Filled
```

### 委託狀態

| Status | 說明 |
|--------|------|
| PendingSubmit | 傳送中 |
| PreSubmitted | 預約單 |
| Submitted | 傳送成功 |
| Failed | 失敗 |
| Cancelled | 已刪除 |
| Filled | 完全成交 |
| PartFilled | 部分成交 |

### 改價 / 改量

```python
api.update_order(trade=trade, price=14450)   # 改價
api.update_order(trade=trade, qty=1)         # 改量（只能減量）
```

### 刪單

```python
api.cancel_order(trade)
```

### 成交資訊

```python
api.update_status(api.futopt_account)
trade.status.deals  # [Deal(seq='000001', price=14400, quantity=3, ts=...)]
```

---

## 4. 即時行情（期貨）

### 訂閱

```python
# Tick
api.quote.subscribe(
    api.Contracts.Futures.TXF["TXF202604"],
    quote_type=sj.constant.QuoteType.Tick,
    version=sj.constant.QuoteVersion.v1,
)

# BidAsk（五檔報價）
api.quote.subscribe(
    contract,
    quote_type=sj.constant.QuoteType.BidAsk,
    version=sj.constant.QuoteVersion.v1,
)

# Quote（Tick + BidAsk 合併）
api.quote.subscribe(
    contract,
    quote_type=sj.constant.QuoteType.Quote,
    version=sj.constant.QuoteVersion.v1,
)
```

### Tick Callback

```python
from shioaji import TickFOPv1, Exchange

def on_tick(exchange: Exchange, tick: TickFOPv1):
    print(tick.code, tick.close, tick.volume)

api.quote.set_on_tick_fop_v1_callback(on_tick)
```

#### TickFOPv1 屬性

| 屬性 | 型別 | 說明 |
|------|------|------|
| code | str | 商品代碼 |
| datetime | datetime | 時間 |
| open | Decimal | 開盤價 |
| close | Decimal | 成交價 |
| high | Decimal | 最高價 |
| low | Decimal | 最低價 |
| volume | int | 成交量 (lot) |
| total_volume | int | 總成交量 |
| amount | Decimal | 成交額 |
| tick_type | int | 1:外盤 2:內盤 0:無法判定 |
| underlying_price | Decimal | 標的物價格 |
| simtrade | int | 試撮 |

### BidAsk Callback

```python
from shioaji import BidAskFOPv1, Exchange

def on_bidask(exchange: Exchange, bidask: BidAskFOPv1):
    print(bidask.code, bidask.bid_price[0], bidask.ask_price[0])

api.quote.set_on_bidask_fop_v1_callback(on_bidask)
```

#### BidAskFOPv1 屬性

| 屬性 | 型別 | 說明 |
|------|------|------|
| code | str | 商品代碼 |
| datetime | datetime | 時間 |
| bid_price | List[Decimal] | 五檔委買價 |
| bid_volume | List[int] | 五檔委買量 |
| ask_price | List[Decimal] | 五檔委賣價 |
| ask_volume | List[int] | 五檔委賣量 |
| bid_total_vol | int | 委買量總計 |
| ask_total_vol | int | 委賣量總計 |
| underlying_price | Decimal | 標的物價格 |

### Quote Callback（Tick + BidAsk 合併）

```python
from shioaji import QuoteFOPv1, Exchange

def on_quote(exchange: Exchange, quote: QuoteFOPv1):
    print(quote.code, quote.close, quote.bid_price[0], quote.ask_price[0])

api.quote.set_on_quote_fop_v1_callback(on_quote)
```

---

## 5. 帳務查詢

### 未實現損益（持倉）

```python
positions = api.list_positions(api.futopt_account)
for p in positions:
    print(p.code, p.direction, p.quantity, p.price, p.last_price, p.pnl)
```

#### FuturePosition 屬性

| 屬性 | 說明 |
|------|------|
| code | 商品代碼 |
| direction | Buy / Sell |
| quantity | 數量 |
| price | 平均成本 |
| last_price | 目前價格 |
| pnl | 損益 |

### 持倉明細

```python
details = api.list_position_detail(api.futopt_account, detail_id=0)
```

---

## 6. 常用商品代碼

| 代碼 | 說明 |
|------|------|
| `TXFR1` | 台指期近月 |
| `MXFR1` | 小台指近月 |
| `TMFR1` | 微台指近月 |
| `TXO{strike}{C/P}{month}` | 台指選擇權 |

### 取得合約

```python
# 近月
api.Contracts.Futures["TXFR1"]
api.Contracts.Futures["TMFR1"]

# 指定月份
api.Contracts.Futures.TXF["TXF202604"]

# 選擇權
api.Contracts.Options.TXO["TXO19000C3"]
```

---

## 7. AI 輔助開發資源

| 資源 | URL |
|------|-----|
| llms.txt | https://sinotrade.github.io/llms.txt |
| llms-full.txt | https://sinotrade.github.io/llms-full.txt |

將 `llms-full.txt` URL 提供給 AI 助手即可獲得完整 Shioaji API 知識。

### 最佳實踐

1. 告訴 AI 你正在使用 Shioaji 台灣交易 API
2. 將 llms-full.txt 提供給 AI 以獲得完整知識
3. 執行前務必檢查 AI 生成的交易程式碼
4. 先在 `simulation=True` 模擬模式中測試

---

## 8. rshioaji (Rust 版本)

> 來源：https://github.com/Sinotrade/rshioaji

- Rust 重寫的 Shioaji，`import shioaji as sj` 完全相容（drop-in replacement）
- 目前 **Alpha 階段**，API 可能變動，不建議 production 使用
- Python binding 版本效能與原版差不多，主要是架構提升
- Pure Rust core 版本（無 Python runtime）才有明顯速度差異，需依交易量申請開放
- 額外提供 HTTP API + SSE streaming，支援任意語言串接
- 安裝：`pip install rshioaji`（會取代 shioaji）
- 目前狀態（2026-04）：login 在 macOS arm64 上會 hang，暫不使用


list_trades() 會回傳當日所有的下單紀錄。建議在查詢前呼叫 update_status() 確保資料最新。
🔍 查詢所有單據
python
api.update_status() # 強制同步最新狀態
trades = api.list_trades()

for trade in trades:
    print(f"[{trade.status.order_datetime}] {trade.contract.code} "
          f"{trade.order.action} {trade.order.quantity}股 "
          f"狀態: {trade.status.status}")


#僅篩選「待成交」的訂單 (未清掉的單)
python
pending_orders = [
    t for t in api.list_trades() 
    if t.status.status in [
        sj.constant.OrderStatus.Submitted, 
        sj.constant.OrderStatus.PartiallyFilled
    ]
]
#今日即時部位 (Account Positions)
#反映今日買賣後的淨部位。
python
positions = api.get_account_pos()
for pos in positions:
    print(f"股票: {pos.code} | 股數: {pos.quantity} | 損益: {pos.pnl}")

#帳戶總庫存 (Portfolio)
包含過去持有至今的股票。
python
portfolio = api.get_portfolio()
for item in portfolio:
    print(f"標的: {item.code} | 庫存股數: {item.quantity}")

⚠️ 4. 防範盤後意外成交 (收盤自動撤單)
為了避免盤中零股未成交導致盤後意外撮合，建議在 13:25 執行以下邏輯：
python
def cancel_all_orders(api):
    api.update_status()
    for trade in api.list_trades():
        # 若狀態為『已送出』或『部分成交』，則撤單
        if trade.status.status in [
            sj.constant.OrderStatus.Submitted, 
            sj.constant.OrderStatus.PartiallyFilled
        ]:
            api.cancel_order(trade)
            print(f"已撤除未成交單: {trade.contract.code}")

# Shioaji 進階交易監控與自動化指南 🚀

本文件提供 Shioaji API 的高可用性實作範例，包含異常處理、多帳號管理與即時通知。

---

## 🛡️ 1. 報錯處理與斷線重連 (Exception Handling)
在自動化交易中，網路波動是常態。使用 `try-except` 搭配迴圈確保查詢不中斷。

```python
import time
import shioaji as sj

def safe_update_status(api, max_retries=3):
    """
    安全更新狀態，失敗時自動重試
    """
    for i in range(max_retries):
        try:
            api.update_status()
            print("✅ 狀態同步成功")
            return True
        except Exception as e:
            print(f"❌ 第 {i+1} 次同步失敗: {e}")
            time.sleep(2) # 等待 2 秒後重試
    return False

# 使用範例
if safe_update_status(api):
    trades = api.list_trades()

2. 多帳號切換 (Multi-Account Management)
# 1. 列出所有關聯帳號
print("您的帳號列表:", api.list_accounts())

# 2. 指定特定帳號 (以證券帳號為例)
# 假設您的帳號 ID 為 'S123456789'
target_account = [acc for acc in api.list_accounts() if acc.account_id == 'S123456789'][0]
api.set_active_account(target_account)

# 3. 查詢該帳號庫存
portfolio = api.get_portfolio()
print(f"目前帳號 {target_account.account_id} 的庫存數: {len(portfolio)}")

結合 Shioaji 訂閱成交事件
@api.on_trade_set()
def on_trade(exchange, trade):
    # 只針對「成交 (Filled)」狀態發送通知
    if trade.status.status == sj.constant.OrderStatus.Filled:
        msg = (
            f"\n🎉 成交通知！\n"
            f"股票: {trade.contract.code} ({trade.contract.name})\n"
            f"動作: {trade.order.action}\n"
            f"價格: {trade.status.filled_avg_price}\n"
            f"張數: {trade.status.filled_share // 1000}"
        )
        send_line_notify(msg, LINE_TOKEN)

# 保持程式運行以接收回報
# api.wait_done()

4. 綜合檢查流程 (Checklist)
檢查項目	建議頻率	目的
api.update_status()	每 5-10 分鐘	同步伺服器與在地端的委託狀態
get_account_pos()	盤中每 1 分鐘	監控即時部位，預防程式跑飛
list_accounts()	啟動時	確認目前操作的是正確的帳號 (避免下錯帳)
on_trade_set()	持續監聽	第一時間捕捉成交動向，不需輪詢 (Polling)

零股陷阱：通知訊息中建議將 filled_share 除以 1000，以區分「張」與「股」。
