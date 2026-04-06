
class StockMonitorDryRun:
    def __init__(self, api, stock_account, capital_limit=10000):
        self.api = api
        self.account = stock_account
        self.capital_limit = capital_limit
        self.positions = {} # {ticker: {"qty": int, "entry_price": float}}

    def calculate_odd_qty(self, price):
        """計算在資金上限內可買進的零股數"""
        # 考慮手續費 (0.1425%) 與基本消費，保守估計
        return int(self.capital_limit // (price * 1.002))

    def on_signal(self, ticker, action, price, reason):
        """模擬信號觸發"""
        if action == "BUY":
            qty = self.calculate_odd_qty(price)
            if qty > 0:
                print(f"🚀 [DRY RUN BUY] {ticker} | Price: {price} | Qty: {qty} shares | Reason: {reason}")
                self.positions[ticker] = {"qty": qty, "entry_price": price}
        
        elif action == "SELL" and ticker in self.positions:
            pos = self.positions[ticker]
            pnl = (price - pos["entry_price"]) * pos["qty"]
            print(f"🏁 [DRY RUN SELL] {ticker} | Price: {price} | PnL: {pnl:+.0f} | Reason: {reason}")
            del self.positions[ticker]

    def monitor_tick(self, ticker):
        """實時價格監控與止損執行"""
        try:
            from datetime import datetime
            now = datetime.now()
            
            # 1. 取得最新報價 (移除不支援的 odd_lot 參數)
            contract = self.api.Contracts.Stocks[ticker]
            snapshot = self.api.snapshots([contract])[0]
            curr_price = snapshot.close
            
            if ticker in self.positions:
                pos = self.positions[ticker]
                entry_price = pos["entry_price"]
                return_pct = (curr_price - entry_price) / entry_price
                
                # 2. 硬性止損 (假設設為 -3%)
                if return_pct <= -0.03:
                    self.on_signal(ticker, "SELL", curr_price, "HARD_STOP_LOSS")
                    return

                # 3. 時間止損 (13:20 強制出場)
                if now.hour == 13 and now.minute >= 20:
                    self.on_signal(ticker, "SELL", curr_price, "TIME_EXIT_CLOSE")
                    return

            print(f"👀 Monitoring {ticker}: Price {curr_price} | Return: {return_pct*100:.2f}%" if ticker in self.positions else f"👀 Monitoring {ticker}: Price {curr_price}")
            
        except Exception as e:
            print(f"Error monitoring {ticker}: {e}")
