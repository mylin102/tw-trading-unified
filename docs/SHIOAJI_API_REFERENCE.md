# Shioaji API Reference

> 來源：https://sinotrade.github.io/zh/
> 版本：2026-04-13（已針對實際 API 驗證）
>
> **已驗證標記**：✅ 已實機驗證｜⚠️ 文檔記載但未驗證｜❌ 不存在/已更名

---

## Part 0: 共用 API（所有商品通用）

### 0.1 登入 / 登出

```python
import shioaji as sj

api = sj.Shioaji()  # 正式環境
# api = sj.Shioaji(simulation=True)  # 模擬環境

api.login(api_key="YOUR_API_KEY", secret_key="YOUR_SECRET_KEY")
api.logout()  # 務必登出，有連線數限制
```

| 參數 | 型別 | 預設 | 說明 |
|------|------|------|------|
| api_key | str | — | API 金鑰（永豐金提供） |
| secret_key | str | — | 密鑰 |
| fetch_contract | bool | True | 是否下載商品檔 |
| contracts_timeout | int | 0 ms | 商品檔 timeout |
| contracts_cb | Callable | None | 商品檔下載完成 callback |
| subscribe_trade | bool | True | 是否訂閱委託/成交回報 |
| receive_window | int | 30000 ms | 登入有效執行時間 |

### 0.2 CA 憑證（股票下單必須）

```python
api.activate_ca(
    ca_path="/path/to/cert.pfx",
    ca_passwd="YOUR_CA_PASSWORD",
    person_id="YOUR_PERSON_ID",
)
```

- 期貨/選擇權下單也需要 CA
- 模擬模式下 `signed` 自動為 `True`

### 0.3 帳號管理

```python
api.list_accounts()        # ✅ 所有帳號列表
api.stock_account          # ✅ 預設股票帳號
api.futopt_account         # ✅ 預設期貨選擇權帳號
api.set_default_account(acc)  # ✅ 切換預設帳號
```

| API | 狀態 | 說明 |
|-----|------|------|
| `api.list_accounts()` | ✅ | 回傳帳號列表 |
| `api.stock_account` | ✅ | property，股票帳號 |
| `api.futopt_account` | ✅ | property，期貨選擇權帳號 |
| `api.set_default_account()` | ✅ | 設定預設帳號 |
| `api.set_active_account()` | ❌ **不存在** | 應使用 `set_default_account()` |

### 0.4 訂單狀態 Enum

```python
sj.constant.Status.Submitted       # 已送出（待成交）
sj.constant.Status.Filled          # 完全成交
sj.constant.Status.PartFilled      # 部分成交
sj.constant.Status.Cancelled       # 已撤單
sj.constant.Status.Failed          # 失敗
sj.constant.Status.PendingSubmit   # 傳送中
sj.constant.Status.PreSubmitted    # 預約單
sj.constant.Status.Inactive        # 未啟動
```

> ⚠️ **重要**：使用 `sj.constant.Status.*`，不是 `sj.constant.OrderState.*`（不存在）
> ⚠️ **重要**：是 `PartFilled` 不是 `PartiallyFilled`

### 0.5 事件監聽

```python
# ✅ 方法 1：裝飾器（推薦）
@api.quote.on_event
def event_cb(event_code, event):
    if event_code == 12:  # RECONNECTING
        print("斷線重連中...")
    elif event_code == 13:  # RECONNECTED
        print("重連成功")
    elif event_code == 20:  # GD flow fail
        print("需重新訂閱")

# ✅ 方法 2：直接設定
@api.quote.set_event_callback
def event_cb(event_code, event):
    pass
```

| Event Code | 名稱 | 說明 |
|------------|------|------|
| 12 | RECONNECTING_NOTICE | 連線斷了，開始重連（最多 50 次） |
| 13 | RECONNECTED_NOTICE | 重連成功 |
| 16 | SUBSCRIPTION_OK | 訂閱成功確認 |
| 20 | REPUBLISH_UNACKED | unknown publisher flow — 需手動重新訂閱 |

---

## Part 1: 股票（Stocks）

### 1.1 行情訂閱

```python
contract = api.Contracts.Stocks["2330"]

# 訂閱 Tick
api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)

# 訂閱 BidAsk
api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)
```

### 1.2 Tick Callback（股票專用）

```python
from shioaji import TickFOPv1, Exchange

def on_tick(exchange: Exchange, tick: TickFOPv1):
    print(tick.code, tick.close, tick.volume)

api.quote.set_on_tick_fop_v1_callback(on_tick)
```

| 屬性 | 型別 | 說明 |
|------|------|------|
| code | str | 股票代碼 |
| datetime | datetime | 時間 |
| open | Decimal | 開盤價 |
| close | Decimal | 成交價 |
| high | Decimal | 最高價 |
| low | Decimal | 最低價 |
| volume | int | 成交量 |
| tick_type | int | 1:外盤 2:內盤 0:無法判定 |
| underlying_price | Decimal | 標的價格 |

### 1.3 BidAsk Callback

```python
from shioaji import BidAskFOPv1, Exchange

def on_bidask(exchange: Exchange, bidask: BidAskFOPv1):
    print(bidask.code, bidask.bid_price[0], bidask.ask_price[0])

api.quote.set_on_bidask_fop_v1_callback(on_bidask)
```

### 1.4 股票下單

```python
contract = api.Contracts.Stocks["2330"]

order = api.Order(
    price=1000,                          # 委託價格
    quantity=1000,                       # 股數
    action=sj.constant.Action.Buy,       # Buy / Sell
    price_type=sj.constant.StockPriceType.LMT,  # LMT(限價) / MKT(市價)
    order_type=sj.constant.OrderType.ROD,       # ROD(當日有效)
    order_lot=sj.constant.StockOrderLot.IntradayOdd,  # 盤中零股，13:30 自動失效
)

trade = api.place_order(contract, order)
```

| 屬性 | 狀態 | 說明 |
|------|------|------|
| `StockPriceType.LMT` | ✅ | 限價 |
| `StockPriceType.MKT` | ✅ | 市價 |
| `StockOrderLot.Common` | ✅ | 整股（1000 股為單位） |
| `StockOrderLot.IntradayOdd` | ✅ | 盤中零股，**13:30 自動失效**（推薦） |
| `StockOrderLot.Odd` | ✅ | 盤後零股，14:30 撮合（⚠️ 有留倉風險） |
| `StockOrderLot.BlockTrade` | ✅ | 鉅額交易 |
| `StockOrderLot.Fixing` | ✅ | 定價 |

> ⚠️ **2026-04-13 Bug Fix**：務必用 `IntradayOdd` 而非 `Odd`。`Odd` 會排隊到 14:30 盤後撮合。

### 1.5 股票庫存查詢

```python
# 今日即時部位（含未實現損益）
positions = api.list_positions(api.stock_account)
for p in positions:
    print(p.code, p.direction, p.quantity, p.price, p.last_price, p.pnl)
```

| 屬性 | 說明 |
|------|------|
| code | 股票代碼 |
| direction | Buy / Sell |
| quantity | 股數 |
| price | 平均成本 |
| last_price | 目前價格 |
| pnl | 未實現損益 |

### 1.6 股票歷史 K 線

```python
contract = api.Contracts.Stocks["2330"]
bars = api.kbars(contract, start="2026-04-01", end="2026-04-13")
df = pd.DataFrame({**bars})
```

> ⚠️ `api.kbars()` 是一次性查詢，不是 streaming。夜盤資料可能尚未推送到伺服器。

---

## Part 2: 期貨（Futures）

### 2.1 常用商品代碼

| 代碼 | 說明 | 取得方式 |
|------|------|----------|
| `TXFR1` | 台指期近月 | ✅ `api.Contracts.Futures["TXFR1"]` |
| `MXFR1` | 小台指近月 | ✅ `api.Contracts.Futures["MXFR1"]` |
| `TMFR1` | 微台指近月 | ✅ `api.Contracts.Futures["TMFR1"]` |
| `TXF202604` | 台指期 2026/04 | ✅ `api.Contracts.Futures.TXF["TXF202604"]` |

### 2.2 期貨/選擇權下單

```python
contract = api.Contracts.Futures["TXFR1"]

order = api.Order(
    price=35500,                                # 委託價格
    quantity=1,                                 # 口數
    action=sj.constant.Action.Buy,              # Buy / Sell
    price_type=sj.constant.FuturesPriceType.LMT,  # LMT(限價) / MKT(市價) / MKP(範圍市價)
    order_type=sj.constant.OrderType.ROD,       # ROD / IOC / FOK
    octype=sj.constant.FuturesOCType.Auto,      # Auto / New / Cover / DayTrade
    account=api.futopt_account,                 # 期貨選擇權帳號
)

trade = api.place_order(contract, order)
```

| 屬性 | 狀態 | 說明 |
|------|------|------|
| `FuturesPriceType.LMT` | ✅ | 限價 |
| `FuturesPriceType.MKT` | ✅ | 市價 |
| `FuturesPriceType.MKP` | ✅ | 範圍市價 |
| `FuturesOCType.Auto` | ✅ | 自動判斷（開/平倉） |
| `FuturesOCType.New` | ✅ | 新倉 |
| `FuturesOCType.Cover` | ✅ | 平倉 |
| `FuturesOCType.DayTrade` | ✅ | 當沖 |

### 2.3 訂單管理

```python
# 更新狀態（查詢前務必呼叫）
api.update_status(api.futopt_account)

# 查詢當日所有委託
trades = api.list_trades()
for trade in trades:
    print(f"[{trade.status.order_datetime}] {trade.contract.code} "
          f"{trade.order.action} {trade.order.quantity}口 "
          f"狀態: {trade.status.status}")

# 篩選待成交訂單
pending = [
    t for t in api.list_trades()
    if t.status.status in [
        sj.constant.Status.Submitted,
        sj.constant.Status.PartFilled,
    ]
]

# 改價 / 改量
api.update_order(trade=trade, price=35500)   # 改價
api.update_order(trade=trade, qty=1)         # 改量（只能減量）

# 撤單
api.cancel_order(trade)
```

| API | 狀態 | 說明 |
|-----|------|------|
| `api.update_status()` | ✅ | 同步委託狀態 |
| `api.list_trades()` | ✅ | 當日所有委託紀錄 |
| `api.update_order()` | ✅ | 改價/改量 |
| `api.cancel_order()` | ✅ | 撤單 |

### 2.4 即時行情（TickFOPv1 / BidAskFOPv1）

同 Part 0 共用機制，訂閱時用 futures contract：

```python
contract = api.Contracts.Futures["TMFR1"]
api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
```

### 2.5 持倉與損益

```python
# 未實現損益
positions = api.list_positions(api.futopt_account)
for p in positions:
    print(p.code, p.direction, p.quantity, p.price, p.last_price, p.pnl)

# 持倉明細
details = api.list_position_detail(api.futopt_account, detail_id=0)
```

| 屬性 | 說明 |
|------|------|
| code | 商品代碼 |
| direction | Buy / Sell |
| quantity | 口數 |
| price | 平均成本 |
| last_price | 目前價格 |
| pnl | 未實現損益 |

### 2.6 成交資訊

```python
api.update_status(api.futopt_account)
trade.status.deals  # [Deal(seq='000001', price=14400, quantity=3, ts=...)]
```

---

## Part 3: 選擇權（Options）

### 3.1 商品代碼

```python
# 台指選擇權
api.Contracts.Options.TXO["TXO19000C3"]  # 履約價 19000 買權 近月

# 動態取得
for contract in api.Contracts.Options.TXO:
    if contract.option_type == "C" and int(contract.strike_price) == 19000:
        print(contract.code, contract.delivery_date)
```

### 3.2 選擇權下單

```python
contract = api.Contracts.Options.TXO["TXO19000C3"]

order = api.Order(
    price=500,                                  # 權利金（點）
    quantity=1,                                 # 口數
    action=sj.constant.Action.Buy,
    price_type=sj.constant.FuturesPriceType.LMT,
    order_type=sj.constant.OrderType.ROD,
    octype=sj.constant.FuturesOCType.Auto,
    account=api.futopt_account,
)

trade = api.place_order(contract, order)
```

> 選擇權使用與期貨相同的 FuturesPriceType / FuturesOCType。

### 3.3 選擇權合約屬性

| 屬性 | 說明 |
|------|------|
| code | 選擇權代碼（如 TXO19000C3） |
| strike_price | 履約價 |
| option_type | C（買權）/ P（賣權） |
| delivery_date | 到期日 |
| underlying_kind | 標的物類型 |
| contract_multiplier | 契約乘數 |

### 3.4 選擇權行情

同期貨，用 TickFOPv1 / BidAskFOPv1 callback：

```python
# 訂閱選擇權 Tick + BidAsk
api.quote.subscribe(option_contract, quote_type=sj.constant.QuoteType.Tick)
api.quote.subscribe(option_contract, quote_type=sj.constant.QuoteType.BidAsk)
```

---

## Part 4: 進階技巧（已驗證）

### 4.1 安全更新狀態（帶 retry）

```python
import time

def safe_update_status(api, max_retries=3):
    """安全更新狀態，失敗時自動重試"""
    for i in range(max_retries):
        try:
            api.update_status()
            return True
        except Exception as e:
            print(f"❌ 第 {i+1} 次同步失敗: {e}")
            time.sleep(2)
    return False
```

### 4.2 收盤自動撤單（防範盤後意外成交）

```python
def cancel_all_stock_orders(api):
    """13:25 執行，撤銷所有未成交的股票委託單"""
    api.update_status()
    for trade in api.list_trades():
        if trade.status.status in [
            sj.constant.Status.Submitted,
            sj.constant.Status.PartFilled,
        ]:
            api.cancel_order(trade)
            print(f"已撤除未成交單: {trade.contract.code}")
```

### 4.3 成交事件監聽

```python
# 注意：api.on_trade_set() 在當前版本 ❌ 不存在
# 改用 quote callback 判斷成交狀態

def on_tick(exchange, tick):
    api.update_status()
    for trade in api.list_trades():
        if trade.status.status == sj.constant.Status.Filled:
            print(f"✅ 成交: {trade.contract.code} @ {trade.status.filled_avg_price}")

api.quote.set_on_tick_fop_v1_callback(on_tick)
```

### 4.4 綜合檢查清單

| 檢查項目 | 建議頻率 | API |
|----------|----------|-----|
| 同步委託狀態 | 每 5-10 分鐘 | `api.update_status()` |
| 監控即時部位 | 盤中每 1 分鐘 | `api.list_positions()` |
| 確認操作帳號 | 啟動時 | `api.list_accounts()` |
| 成交事件監聽 | 持續 | `quote.set_on_tick_fop_v1_callback()` |
| 連線狀態監控 | 持續 | `@api.quote.on_event` |

---

## Part 5: 不存在的 API（常見錯誤）

| API | 狀態 | 正確做法 |
|-----|------|----------|
| `api.get_account_pos()` | ❌ 不存在 | 用 `api.list_positions()` |
| `api.get_portfolio()` | ❌ 不存在 | 用 `api.list_positions()` + 歷史帳務追蹤 |
| `api.set_active_account()` | ❌ 不存在 | 用 `api.set_default_account()` |
| `api.wait_done()` | ❌ 不存在 | 用 `time.sleep()` + callback 判斷 |
| `api.on_trade_set()` | ❌ 不存在 | 用 tick callback + `update_status()` |
| `sj.constant.OrderState.Submitted` | ❌ 不存在 | 用 `sj.constant.Status.Submitted` |
| `sj.constant.Status.PartiallyFilled` | ❌ 不存在 | 用 `sj.constant.Status.PartFilled` |
| `sj.constant.OrderStatus.Submitted` | ❌ 不存在 | 用 `sj.constant.Status.Submitted` |

---

## Part 6: 外部資源

| 資源 | URL |
|------|-----|
| 官方文件 | https://sinotrade.github.io/zh/ |
| llms.txt | https://sinotrade.github.io/llms.txt |
| llms-full.txt | https://sinotrade.github.io/llms-full.txt |
| GitHub | https://github.com/Sinotrade/Shioaji |
| Event Callback 說明 | https://sinotrade.github.io/tutor/callback/event_cb/ |

### 最佳實踐

1. 告訴 AI 你正在使用 Shioaji 台灣交易 API
2. 將 llms-full.txt 提供給 AI 以獲得完整知識
3. 執行前務必檢查 AI 生成的交易程式碼
4. 先在 `simulation=True` 模擬模式中測試
5. 所有訂單狀態比較務必用 `sj.constant.Status.*`

---

## Part 7: rshioaji (Rust 版本)

> 來源：https://github.com/Sinotrade/rshioaji

- Rust 重寫的 Shioaji，`import shioaji as sj` 完全相容（drop-in replacement）
- 目前 **Alpha 階段**，API 可能變動，不建議 production 使用
- Python binding 版本效能與原版差不多
- 目前狀態（2026-04）：login 在 macOS arm64 上會 hang，暫不使用

---

*最後更新：2026-04-13 — 所有標記 ✅ 的 API 已實機驗證*
