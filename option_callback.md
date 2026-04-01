1. 關鍵 Callback 接口
與期貨類似，但選擇權與期貨共用 fop (Futures and Options) 相關的接口。
成交資料：set_on_tick_fop_v1_callback
五檔報價：set_on_bidask_fop_v1_callback
2. Python 範例：訂閱 TXO
選擇權的代碼規則通常是 商品代碼 + 履約價 + 買賣權代號 (例如：TXO18000L4 代表 18000 點的 12 月 Call)。

import shioaji as sj

# 初始化與登入 (省略)
# 1. 設定 CallBack (使用 on_tick_fop_v1_callback)
# 2. 定義合約與訂閱 (範例代碼需依實時行情調整)
option_contract = api.Contracts.Options.TXO.TXO20241218000C
api.quote.subscribe(option_contract, quote_type=sj.constant.QuoteType.Tick)

3. 如何快速找到 TXO 代碼？
選擇權合約非常多，建議使用篩選功能：
列出所有 TXO：print(api.Contracts.Options.TXO)
動態篩選：根據 delivery_month 或 strike_price 尋找所需物件。
4. 下單注意事項 (TXO Order)
下單時須指定 action (買/賣) 與 option_right (Call/Put)。

