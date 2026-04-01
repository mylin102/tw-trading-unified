1. 實作 Bid/Ask Callback 監控 IV
我們使用 set_on_bidask_fop_v1_callback 來捕捉掛價變動：
from shioaji import BidAskFopv1, Exchange

# 1. 定義處理五檔的 Callback
def on_bidask_fop_v1(exchange: Exchange, bidask: BidAskFopv1):
    # 取第一檔買賣價
    bid_price = bidask.bid_price[0]
    ask_price = bidask.ask_price[0]
    
    # 計算中價 (Mid Price)，避免成交稀疏導致 IV 停滯
    if bid_price > 0 and ask_price > 0:
        mid_price = (bid_price + ask_price) / 2
        
        # 這裡同步抓取目前的小台價格 (S)
        # S = latest_mtx_price 
        
        # 2. 計算即時 IV
        # current_iv = calculate_iv(mid_price, S, K=22000, T=remaining_days)
        # print(f"2026/04 22000C 即時 IV (中價): {current_iv:.2%}")
        
        # 3. 判斷噴發邏輯
        # if current_iv > threshold:
        #    alert("偵測到隱含波動率異常噴發！")

# 2. 綁定接口
api.quote.set_on_bidask_fop_v1_callback(on_bidask_fop_v1)

# 3. 訂閱五檔報價 (注意 quote_type 要設為 BidAsk)
target_txo = api.Contracts.Options.TXO.TXO20260422000C
api.quote.subscribe(
    target_txo, 
    quote_type=sj.constant.QuoteType.BidAsk, 
    version=sj.constant.QuoteVersion.v1
)
2. 凌晨監控優勢
即時感應：當市場發生突發利空，賣方會瞬間抽單或抬高掛價，此時成交還沒發生，但 mid_price 會立刻拉升，你的 IV 監控器會比別人早幾秒發現噴發。
穩定性：成交價 (Tick) 容易因為單筆大單產生極端偏離，五檔中價相對平滑，計算出的 IV 曲線更適合做自動化警示。
import shioaji as sj
from datetime import datetime
import math
from py_vollib.black_scholes.implied_volatility import implied_volatility

class IVMonitor:
    def __init__(self, strike, expiry_date, flag='c'):
        self.strike = strike
        self.expiry_date = expiry_date
        self.flag = flag
        self.underlying_price = 0.0
        self.option_price = 0.0
        self.last_iv = 0.0
        self.threshold = 0.02  # IV 絕對值變動 2% 警示

    def get_t(self):
        # 計算到期時間 (年化)
        now = datetime.now()
        delta = self.expiry_date - now
        # 2026/04 凌晨監控，秒數非常精確
        return max(0.00001, delta.total_seconds() / (365 * 24 * 3600))

    def calculate_and_check(self):
        if self.underlying_price <= 0 or self.option_price <= 0:
            return

        try:
            t = self.get_t()
            # 計算即時 IV (假設利率 r = 0.02)
            current_iv = implied_volatility(self.option_price, self.underlying_price, 
                                            self.strike, t, 0.02, self.flag)
            
            # 監控噴發邏輯
            if self.last_iv > 0:
                diff = current_iv - self.last_iv
                if diff > self.threshold:
                    print(f"🔥 IV 噴發警報！價格: {self.option_price}, IV: {current_iv:.2%}, 較上一筆增加: {diff:.2%}")
            
            self.last_iv = current_iv
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 標的: {self.underlying_price} | IV: {current_iv:.2%}")
        except:
            pass

# --- 2026/04 實戰啟動 ---

api = sj.Shioaji()
api.login("API_KEY", "SECRET_KEY")

# 1. 設定監控參數 (2026/04 結算日為 4/15)
monitor = IVMonitor(strike=33000, expiry_date=datetime(2026, 4, 15, 13, 30))

# 2. 定義小台 (MTX) Callback - 更新標的價 (S)
@api.on_tick_futu_v1()
def on_tick_mtx(exchange, tick):
    monitor.underlying_price = tick.close
    monitor.calculate_and_check()

# 3. 定義選擇權 (TXO) BidAsk Callback - 更新權利金 (P)
@api.on_bidask_fop_v1()
def on_bidask_txo(exchange, bidask):
    # 使用中價計算，解決凌晨成交稀疏問題
    if bidask.bid_price > 0 and bidask.ask_price > 0:
        monitor.option_price = (bidask.bid_price + bidask.ask_price) / 2
        monitor.calculate_and_check()

# 4. 取得合約並訂閱
# 2026/04 近月小台
mtx_contract = api.Contracts.Futures.MTX.MTX202604
# 2026/04 33000 Call
txo_contract = api.Contracts.Options.TXO.TXO20260433000C

api.quote.subscribe(mtx_contract, quote_type=sj.constant.QuoteType.Tick)
api.quote.subscribe(txo_contract, quote_type=sj.constant.QuoteType.BidAsk)

print("2026/04 夜盤 IV 監控已啟動...")

這份程式碼的優點：
解決凌晨沒資料：完全不依賴 kbars，只要夜盤有報價跳動（BidAsk），IV 就會即時更新。
自動化計算 T：使用 datetime.now()，精確計算到 2026/04/15 結算前的每一秒。
中價機制：在 on_bidask_fop_v1 裡算中價，比等成交 Tick 更敏感，適合買方抓「噴發瞬間」。
