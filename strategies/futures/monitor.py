"""
Futures monitor — full strategy from daily_simulation.
Accepts an injected Shioaji API instance (no internal login).
"""
import sys
import os
import time
import yaml
import traceback
from collections import deque
from datetime import datetime, timedelta
import pandas as pd
from rich.console import Console

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from squeeze_futures.engine.constants import get_point_value
from squeeze_futures.engine.simulator import PaperTrader
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from squeeze_futures.data.shioaji_client import ShioajiClient
from squeeze_futures.data.data_storage import save_trade

try:
    from squeeze_futures.report.notifier import send_email_notification
except ImportError:
    send_email_notification = None

console = Console()


def _check_trend_breakout_signal(df_5m, df_15m):
    try:
        from squeeze_futures.engine.trend_breakout import check_trend_breakout
    except ImportError:
        return {"trend_long": False, "trend_short": False, "reasons": []}
    result = {"trend_long": False, "trend_short": False, "reasons": []}
    if len(df_5m) >= 20:
        b = check_trend_breakout(df_5m, lookback=20, ma_length=20, compare_bars=5, slope_threshold=0.1)
        if b["long_signal"]:
            result["trend_long"] = True
            result["reasons"].extend([f"5m: {r}" for r in b["long_reasons"]])
        if b["short_signal"]:
            result["trend_short"] = True
            result["reasons"].extend([f"5m: {r}" for r in b["short_reasons"]])
    return result


class FuturesMonitor:
    def __init__(self, api, config_path: str, dry_run: bool = False):
        self.api = api
        self.dry_run = dry_run
        self.cfg = self._load_config(config_path)
        self.ticker = "TMF"
        self.contract = None
        self._running = False

        # Wrap injected api into ShioajiClient without re-login
        self.client = ShioajiClient.__new__(ShioajiClient)
        self.client.api = api
        self.client.is_logged_in = not dry_run
        self.client._tick_callbacks = {}
        self.client._kbar_callbacks = {}
        self.client._latest_kbars = {}

        # Strategy config
        self.STRATEGY = self.cfg.get("strategy", {})
        self.RISK = self.cfg.get("risk_mgmt", {})
        self.MGMT = self.cfg.get("trade_mgmt", {})
        self.EXEC = self.cfg.get("execution", {})
        self.MONITOR = self.cfg.get("monitoring", {})
        self.PB = self.STRATEGY.get("pullback", {})
        self.TP = self.STRATEGY.get("partial_exit", {})
        self.FILTER_MODE = self.STRATEGY.get("regime_filter", "mid")
        self.ATR_MULT = self.RISK.get("atr_multiplier", 0.0)
        self.ATR_LENGTH = self.RISK.get("atr_length", 14)
        self.POLL_INTERVAL = self.MONITOR.get("poll_interval_secs", 30)
        self.PB_CONFIRM_BARS = self.MONITOR.get("pb_confirmation_bars", 12)
        self.PB_ARGS = {
            "ema_fast": self.PB.get("ema_fast", 20),
            "ema_slow": self.PB.get("ema_slow", 60),
            "pb_buffer": self.PB.get("buffer", 1.002),
        }
        self.live_trading = self.cfg.get("live_trading", False)
        self.cooldown_bars = self.cfg.get("cooldown_bars", self.STRATEGY.get("cooldown_bars", 8))
        self.cooldown_until = 0

        # Squeeze Failure Counter mode
        self.COUNTER = self.STRATEGY.get("counter_mode", {})
        self.counter_enabled = self.COUNTER.get("enabled", False)
        self.counter_auto_regime = self.COUNTER.get("auto_regime", True)
        self.counter_confirm_bars = self.COUNTER.get("confirm_bars", 5)
        self.counter_atr_sl_mult = self.COUNTER.get("atr_sl_mult", 1.0)
        self.counter_exit_vwap = self.COUNTER.get("exit_on_vwap", True)
        # Failure detection state: tracks pending squeeze fire
        self._fire_pending_dir = 0   # +1=bullish fire, -1=bearish fire
        self._fire_bar_idx = 0
        self._fire_high = 0.0
        self._fire_low = 0.0
        self._bar_counter = 0        # monotonic bar counter for fire tracking
        self._vwap_violation_bars = 0  # VWAP exit debounce counter
        self.last_tick_at = time.time()  # [gstack] 數據新鮮度追蹤

        # Trader
        self.trader = PaperTrader(
            ticker=self.ticker,
            initial_balance=self.EXEC.get("initial_balance", 100000),
            point_value=get_point_value(self.ticker),
            fee_per_side=self.EXEC.get("broker_fee_per_side", 20),
            exchange_fee_per_side=self.EXEC.get("exchange_fee_per_side", 0),
            tax_rate=self.EXEC.get("tax_rate", 0),
        )
        self.has_tp1_hit = False
        self.last_processed_bar = None
        self._last_entry_reason = None
        self._safety_stop_trade = None  # Exchange-side safety stop order
        self._last_bar_ts = None  # [Wave 1 optimization] Cache bar timestamp to avoid repeated floor() calls

    def _load_config(self, path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _get_tick_bars_df(self):
        """[Wave 2 optimization] Lazy DF conversion: rebuild cache only on new bar."""
        if self._tick_bars_cache is None and len(self._tick_bars_deque) > 0:
            # Build DataFrame from deque
            records = list(self._tick_bars_deque)
            self._tick_bars_cache = pd.DataFrame({
                "Open": [r["open"] for r in records],
                "High": [r["high"] for r in records],
                "Low": [r["low"] for r in records],
                "Close": [r["close"] for r in records],
                "Volume": [r["volume"] for r in records],
            }, index=[r["ts"] for r in records])
        return self._tick_bars_cache if self._tick_bars_cache is not None else pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def setup(self):
        # Tick-based bar builder (Initialize always to avoid AttributeError in dry_run)
        # [Wave 2 optimization] Use deque for O(1) append/trim instead of DataFrame.loc + slicing
        self._tick_bars_deque = deque(maxlen=300)
        self._tick_bars_cache = None  # Cached DF for indicator calculations
        self._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}

        if self.dry_run:
            console.print("[yellow][FuturesMonitor] dry-run: skipping contract fetch[/yellow]")
            return True

        # [Bug fix] 取得 TMF 合約，讓 on_tick 能正確接收 tick
        try:
            self.contract = self.api.Contracts.Futures.TMF.TMFR1
            if self.contract:
                console.print(f"[green][FuturesMonitor] TMF contract: {self.contract.code}[/green]")
        except Exception:
            pass

        # Pre-fill from kbars if available
        try:
            df = self.client.get_kline(self.ticker, interval="5m")
            if not df.empty:
                # Convert pre-filled bars to deque format
                for _, row in df[["Open", "High", "Low", "Close", "Volume"]].iterrows():
                    bar_dict = {
                        "open": row["Open"],
                        "high": row["High"],
                        "low": row["Low"],
                        "close": row["Close"],
                        "volume": row["Volume"],
                        "ts": row.name,  # DataFrame index is timestamp
                    }
                    self._tick_bars_deque.append(bar_dict)
                self._tick_bars_cache = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                console.print(f"[green][FuturesMonitor] pre-filled {len(self._tick_bars_deque)} bars from kbars[/green]")
        except Exception:
            pass
        return True

    def on_tick(self, exchange, tick):
        self.last_tick_at = time.time()  # [gstack] 更新數據更新時間
        # Accept tick if it matches contract code OR contract category (TMF)
        if self.contract:
            if tick.code != self.contract.code and not tick.code.startswith("TMF"):
                return
        # Build 5m bars from ticks
        price = float(tick.close)
        vol = int(getattr(tick, "volume", 1))
        
        # [Wave 1 optimization] Use integer time bucketing to avoid expensive pd.Timestamp().floor()
        # Only compute Timestamp when bar changes (every 5 minutes)
        tick_ts = pd.Timestamp(tick.datetime)
        ts_int = int(tick_ts.timestamp() / 300) * 300
        
        bar = self._current_bar
        if bar["ts"] is None or ts_int != self._last_bar_ts:
            # Save previous bar to deque (O(1) append, auto-trims to maxlen=300)
            if bar["ts"] is not None and bar["open"] > 0:
                bar_dict = {
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ts": bar["ts"],
                }
                self._tick_bars_deque.append(bar_dict)
                # Invalidate DF cache (will be rebuilt lazily on next indicator calc)
                self._tick_bars_cache = None
            # Convert to Timestamp only when bar changes
            ts = pd.Timestamp(ts_int, unit='s')
            bar["ts"] = ts
            self._last_bar_ts = ts_int
            bar["open"] = bar["high"] = bar["low"] = bar["close"] = price
            bar["volume"] = vol
        else:
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += vol
            return
        cb = self.client._tick_callbacks.get(tick.code)
        if cb:
            cb(exchange, tick)

    # ── Safety Stop (exchange-side protection) ──
    def _place_safety_stop(self, entry_price, direction, lots, stop_loss_pts):
        """Place a far-limit order at exchange as safety stop for disconnect protection."""
        if not self.live_trading or self.dry_run or not self.contract or not self.api:
            return
        try:
            import shioaji as sj
            # Safety stop is wider than strategy stop (2x) to avoid premature fills
            safety_pts = stop_loss_pts * 2 if stop_loss_pts > 0 else 200
            if direction == "LONG":
                safety_price = entry_price - safety_pts
                action = sj.constant.Action.Sell
            else:
                safety_price = entry_price + safety_pts
                action = sj.constant.Action.Buy

            order = self.api.Order(
                price=safety_price,
                quantity=lots,
                action=action,
                price_type=sj.constant.FuturesPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                octype=sj.constant.FuturesOCType.Cover,
                account=self.api.futopt_account,
            )
            trade = self.api.place_order(self.contract, order)
            if trade and trade.status.status != sj.constant.Status.Failed:
                self._safety_stop_trade = trade
                console.print(f"[bold yellow]🛡️ Safety stop placed: {action.value} @ {safety_price:.0f} ({safety_pts:.0f}pts from entry)[/bold yellow]")
            else:
                console.print("[red]Safety stop failed to place[/red]")
        except Exception as e:
            console.print(f"[yellow]Safety stop error: {e}[/yellow]")

    def _cancel_safety_stop(self):
        """Cancel the exchange-side safety stop after normal exit."""
        if not self._safety_stop_trade or not self.api:
            return
        try:
            self.api.cancel_order(self._safety_stop_trade)
            console.print("[dim]🛡️ Safety stop cancelled[/dim]")
        except Exception as e:
            console.print(f"[yellow]Safety stop cancel error: {e}[/yellow]")
        self._safety_stop_trade = None

    # ── Margin check ──
    def _margin_sufficient(self):
        """Check if account has enough margin before placing entry order."""
        try:
            margin = self.api.margin(self.api.futopt_account)
            equity = margin.equity
            reserve_pct = 0.20  # 保留 20% 不動用
            available = equity * (1 - reserve_pct)
            required = margin.initial_margin if margin.initial_margin > 0 else 17000  # TMF 一口約 17,000
            if available < required:
                console.print(f"[red]Margin check: equity={equity:.0f} available={available:.0f} < required={required:.0f}[/red]")
                return False
            console.print(f"[dim]Margin OK: equity={equity:.0f} available={available:.0f}[/dim]")
            return True
        except Exception as e:
            console.print(f"[yellow]Margin check failed: {e} — allowing order[/yellow]")
            return True  # API 查詢失敗不擋單，讓交易所擋

    # ── Trade execution ──
    def _execute_trade(self, signal, price, ts, lots, *, stop_loss=None, break_even_trigger=None, reason=None):
        action = None
        if signal == "BUY":
            action = "Buy"
        elif signal == "SELL":
            action = "Sell"
        elif signal in ("EXIT", "PARTIAL_EXIT"):
            if self.trader.position == 0:
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "no_position", "lots": lots})
                return None
            action = "Sell" if self.trader.position > 0 else "Buy"

        live_ready = self.live_trading and not self.dry_run and self.contract is not None
        if live_ready and action is not None:
            # 進場前檢查保證金（出場不擋）
            if signal in ("BUY", "SELL"):
                if not self._margin_sufficient():
                    console.print(f"[red][FuturesMonitor] ⛔ 保證金不足，取消 {signal}[/red]")
                    from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                    save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "margin_insufficient", "lots": lots})
                    return None
            # 出場前先刪 safety stop，避免庫存不足
            if signal in ("EXIT", "PARTIAL_EXIT"):
                self._cancel_safety_stop()
            trade = self.client.place_order(self.contract, action=action, quantity=lots)
            if trade is None:
                console.print(f"[red][FuturesMonitor] Live order failed: {signal} {lots}[/red]")
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "api_order_failed", "lots": lots})
                return None

        # 計算 PnL（出場時，含手續費+稅金）
        pnl_pts = 0
        pnl_cash = 0
        friction_cost = 0
        direction = ""
        if signal == "BUY":
            direction = "LONG"
        elif signal == "SELL":
            direction = "SHORT"
        elif signal in ("EXIT", "PARTIAL_EXIT") and self.trader.entry_price > 0:
            direction = "LONG" if self.trader.position > 0 else "SHORT"
            sign = 1 if self.trader.position > 0 else -1
            pnl_pts = (price - self.trader.entry_price) * sign
            gross = pnl_pts * self.trader.point_value * lots
            fee = self.trader.fee_per_side * 2 * lots
            exch_fee = self.trader.exchange_fee_per_side * 2 * lots
            tax = (self.trader.entry_price + price) * self.trader.point_value * self.trader.tax_rate * lots
            friction_cost = fee + exch_fee + tax
            pnl_cash = gross - friction_cost

        if signal in ("BUY", "SELL"):
            self._last_entry_reason = reason
        result = self.trader.execute_signal(
            signal, price, ts, lots=lots,
            max_lots=self.MGMT.get("max_positions", 2),
            stop_loss=stop_loss, break_even_trigger=break_even_trigger, exit_reason=reason,
        )
        if not result:
            from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
            save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "papertrader_rejected", "lots": lots})
            return None
        # 信號成功執行，記錄審計軌跡
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
        save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "", "lots": lots, "result": result})
        save_trade({"type": signal, "timestamp": ts, "price": price, "lots": lots,
                    "direction": direction, "pnl_pts": round(pnl_pts, 1),
                    "pnl_cash": round(pnl_cash, 0), "friction_cost": round(friction_cost, 0),
                    "reason": reason or ""})
        d = "🟢 BUY" if signal == "BUY" else "🔴 SELL" if signal == "SELL" else "⚪ EXIT"
        friction_note = f" (摩擦成本 {friction_cost:.0f} TWD)" if friction_cost > 0 else ""
        console.print(f"[bold green][FuturesMonitor] [{ts}] {d} {lots} lots @ {price:.0f}  {result}{friction_note}[/bold green]")
        # Safety stop management
        if live_ready:
            if signal in ("BUY", "SELL"):
                direction = "LONG" if signal == "BUY" else "SHORT"
                sl_pts = stop_loss if stop_loss else self.RISK.get("stop_loss_pts", 60)
                self._place_safety_stop(price, direction, lots, sl_pts)
            if send_email_notification:
                    send_email_notification(
                        f"[TMF] {signal} {lots} lots @ {price:.0f}",
                        f"{d} {lots} lots @ {price:.0f}\n{result}",
                    )
        return result

    def _check_stop_loss(self, ts, price):
        if self.trader.position == 0:
            return None
            
        self.RISK.get("stop_loss_pts", 60)
        # 如果有設定 ATR 倍數，則使用動態停損
        if self.ATR_MULT > 0:
            # 這裡需要傳入當前的 df_5m 來算最新的 ATR
            # 但為了效率，我們可以假設在 _strategy_tick 中已經算好了，或者這裡重新算
            # 這裡簡單處理：如果 trader 有 current_stop_loss 就用它
            pass

        if self.trader.position > 0 and self.trader.current_stop_loss and price <= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", price, ts, abs(self.trader.position), reason="STOP_LOSS")
        if self.trader.position < 0 and self.trader.current_stop_loss and price >= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", price, ts, abs(self.trader.position), reason="STOP_LOSS")
        return None

    def _detect_squeeze_failure(self, last_5m, df_5m):
        """
        Detect squeeze breakout failure → return counter signal.
        Returns: "COUNTER_BUY", "COUNTER_SELL", or None
        """
        fired = last_5m.get("fired", False)
        momentum = last_5m.get("momentum", 0)
        close = last_5m["Close"]

        # New fire event
        if fired:
            self._fire_pending_dir = 1 if momentum > 0 else -1
            self._fire_bar_idx = self._bar_counter
            self._fire_high = close
            self._fire_low = close
            return None

        if self._fire_pending_dir == 0:
            return None

        bars_since = self._bar_counter - self._fire_bar_idx
        self._fire_high = max(self._fire_high, close)
        self._fire_low = min(self._fire_low, close)

        # Expire
        if bars_since > self.counter_confirm_bars:
            self._fire_pending_dir = 0
            return None

        if bars_since < 1:
            return None

        # Failure validation
        recent_high = last_5m.get("recent_high", close)
        recent_low = last_5m.get("recent_low", close)
        mom_velo = last_5m.get("mom_velo", 0)
        vwap = last_5m.get("vwap", close)

        if self._fire_pending_dir == 1:  # Bullish fire failed?
            no_new_high = close < recent_high
            velo_reversed = mom_velo <= 0
            vwap_reject = close < vwap
            if no_new_high and (velo_reversed or vwap_reject):
                self._fire_pending_dir = 0
                return "COUNTER_SELL"
        else:  # Bearish fire failed?
            no_new_low = close > recent_low
            velo_reversed = mom_velo >= 0
            vwap_reject = close > vwap
            if no_new_low and (velo_reversed or vwap_reject):
                self._fire_pending_dir = 0
                return "COUNTER_BUY"

        return None

    def _is_ranging_regime(self, df_5m):
        """Auto-detect ranging market: recent bars flip bullish_align frequently."""
        if len(df_5m) < 20:
            return False
        recent = df_5m["bullish_align"].iloc[-20:]
        flips = (recent != recent.shift(1)).sum()
        return flips >= 4  # 20 bars 內翻轉 4 次以上 → 盤整

    def _save_bar(self, row, score, regime):
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs", "market_data")
        os.makedirs(log_dir, exist_ok=True)
        
        # 交易日日期邏輯:
        # 夜盤 15:00~23:59 → 算明天的交易日
        # 凌晨 00:00~05:00 → 算前一天的交易日
        # 日盤 08:45~13:45 → 算今天
        now = datetime.now()
        if now.hour < 5:
            date_str = (now - timedelta(days=1)).strftime('%Y%m%d')
        elif now.hour >= 15:
            date_str = (now + timedelta(days=1)).strftime('%Y%m%d')
        else:
            date_str = now.strftime('%Y%m%d')
        
        tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
        path = os.path.join(log_dir, f"{self.ticker}_{date_str}{tag}_indicators.csv")
        
        data = {
            "timestamp": row.name, 
            "Open": row["Open"], "High": row["High"], "Low": row["Low"], "Close": row["Close"], 
            "open": row["Open"], "high": row["High"], "low": row["Low"], "close": row["Close"],
            "Volume": row.get("Volume", 0), "Amount": row.get("Amount", 0),
            "volume": row.get("Volume", 0), "amount": row.get("Amount", 0),
            "vwap": row["vwap"], "score": score, "momentum": row.get("momentum", 0),
            "sqz_on": row["sqz_on"], "mom_state": row["mom_state"], "regime": regime,
            "bull_align": row["bullish_align"], "bear_align": row["bearish_align"],
            "bullish_align": row["bullish_align"], "bearish_align": row["bearish_align"],
            "in_pb_zone": row.get("in_bull_pb_zone", False) or row.get("in_bear_pb_zone", False),
            "fired": row.get("fired", False),
        }
        
        file_exists = os.path.exists(path)
        if file_exists:
            # 讀取既有 header，對齊欄位（缺的填 NaN）
            with open(path, 'r') as f:
                existing_cols = f.readline().strip().split(',')
            row_dict = {c: data.get(c, '') for c in existing_cols}
            pd.DataFrame([row_dict]).to_csv(path, mode="a", index=False, header=False)
        else:
            pd.DataFrame([data]).to_csv(path, mode="a", index=False, header=True)

    # ── Main strategy loop ──
    def run(self):
        self._running = True
        mode = "dry-run" if self.dry_run else ("LIVE" if self.live_trading else "PAPER")
        # Recover position from API on restart
        if not self.dry_run and self.api:
            try:
                positions = self.api.list_positions(self.api.futopt_account)
                for p in positions:
                    if self.contract and getattr(p, 'code', '') == self.contract.code:
                        qty = p.quantity if str(p.direction) == 'Buy' else -p.quantity
                        self.trader.position = qty
                        self.trader.entry_price = float(p.price)
                        console.print(f"[bold cyan]♻️ Recovered futures position: {qty} @ {p.price}[/bold cyan]")
                        break
            except Exception as e:
                console.print(f"[yellow]Futures position recovery failed: {e}[/yellow]")
        console.print(f"[green][FuturesMonitor] started ({mode})[/green]")

        while self._running:
            try:
                self._strategy_tick()
            except Exception as e:
                traceback.print_exc()
                console.print(f"[red][FuturesMonitor] error: {e}[/red]")
                print(f"[TRACEBACK] {traceback.format_exc()}", flush=True)
            time.sleep(self.POLL_INTERVAL)

    def stop(self):
        self._running = False

    def _strategy_tick(self):
        # 市場時間檢查
        now = datetime.now()
        h = now.hour
        is_day = 8 <= h < 14
        is_night = h >= 15 or h < 5
        
        # 在 dry_run 模式下跳過時間檢查，方便測試
        if not self.dry_run and not (is_day or is_night):
            return

        # 1. Fetch multi-timeframe data
        processed = {}
        if not self.dry_run:
            # [gstack] 降低累積門檻，從 100 降到 30，只要能算出指標就顯示
            # [Wave 2 optimization] Use lazy DF conversion from deque
            df_base = self._get_tick_bars_df()
            if len(df_base) >= 30:
                processed["5m"] = calculate_futures_squeeze(df_base, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                
                # Resample for higher timeframes
                for tf, rule in [("15m", "15min"), ("1h", "1h")]:
                    res = df_base.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
                    if len(res) >= 20:
                        processed[tf] = calculate_futures_squeeze(res, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
            
            # 如果數據不足，才去調用 API
            if "5m" not in processed:
                try:
                    df = self.client.get_kline(self.ticker, interval="5m")
                    if not df.empty:
                        # [gstack] 確保 K 線長度足以計算指標 (BB_Length=20)
                        if len(df) >= 20:
                            processed["5m"] = calculate_futures_squeeze(df, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                        else:
                            processed["5m"] = df # 只有 OHLCV 也行，至少 Dashboard 有畫面
                except Exception as e:
                    console.print(f"[yellow][FuturesMonitor] api.kbars failed: {e}[/yellow]")

        # 只要有 5m 數據，不論有沒有指標，都應該寫入
        if "5m" not in processed:
            # 最後一招：如果連 api 都沒有，用目前手上剛湊出的 current_bar 墊檔
            if self._current_bar["ts"] is not None and self._current_bar["open"] > 0:
                df_tmp = pd.DataFrame([self._current_bar]).set_index("ts")
                df_tmp.columns = ["Open", "High", "Low", "Close", "Volume"]
                processed["5m"] = df_tmp
            else:
                return

        df_5m = processed["5m"]
        last_5m = df_5m.iloc[-1]
        
        # fallback for MTF
        df_15m = processed.get("15m", df_5m)
        last_15m = df_15m.iloc[-1]
        
        # 指標預設值
        score = 0.0
        regime = "NORMAL"
        
        # 只有在數據充足時才算 MTF Score
        if "15m" in processed:
            score_data = calculate_mtf_alignment(processed, weights=self.STRATEGY.get("weights", {"5m": 0.4, "15m": 0.4, "1h": 0.2}))
            score = score_data["score"]
            regime = "STRONG" if last_5m.get("opening_bullish") else ("WEAK" if last_5m.get("opening_bearish") else "NORMAL")

        last_price = last_5m["Close"]
        vwap = last_5m.get("vwap", last_price)
        timestamp = last_5m.name

        # Log bar (即便每分鐘更新也行，存檔邏輯會處理)
        if self.last_processed_bar != timestamp:
            self._save_bar(last_5m, score, regime)
            self.last_processed_bar = timestamp
            self._bar_counter += 1
            console.print(f"[bold blue][FuturesMonitor] New Bar: {timestamp} close={last_price:.0f} score={score:.1f}[/bold blue]")

        # 如果是 dry_run，計算完指標並存檔後就結束，不執行交易邏輯
        if self.dry_run:
            return

        # 2. Position management
        if self.trader.position != 0:
            self.trader.update_trailing_stop(last_price)
            # TP1
            if self.TP.get("enabled") and abs(self.trader.position) == self.MGMT.get("lots_per_trade", 2) and not self.has_tp1_hit:
                pnl_pts = (last_price - self.trader.entry_price) * (1 if self.trader.position > 0 else -1)
                if pnl_pts >= self.TP.get("tp1_pts", 50):
                    msg = self._execute_trade("PARTIAL_EXIT", last_price, timestamp, self.TP.get("tp1_lots", 1), reason="TP1")
                    if msg:
                        self.has_tp1_hit = True
                        self.trader.current_stop_loss = self.trader.entry_price
                        self.cooldown_until = self.cooldown_bars  # 分批平倉也重置冷卻

            stop_msg = self._check_stop_loss(timestamp, last_price)
            if not stop_msg:
                vwap_exit = self.RISK.get("exit_on_vwap") or (self.counter_exit_vwap and self._last_entry_reason == "COUNTER")
                vwap_confirm_needed = self.RISK.get("exit_vwap_confirm_bars", 0)
                if vwap_exit:
                    # Check if price is beyond VWAP
                    vwap_violated = (
                        (self.trader.position > 0 and last_price < vwap) or
                        (self.trader.position < 0 and last_price > vwap)
                    )
                    if vwap_violated:
                        self._vwap_violation_bars += 1
                    else:
                        self._vwap_violation_bars = 0  # Reset if price returns to favorable side

                    if self._vwap_violation_bars >= vwap_confirm_needed:
                        stop_msg = self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="VWAP")
                        self._vwap_violation_bars = 0  # Reset after exit
            if stop_msg:
                self.has_tp1_hit = False
                self.cooldown_until = self.cooldown_bars # 觸發停損/平倉後進入冷卻
            return  # don't enter same bar as exit

        # 3. Entry logic (with cooldown check)
        if self.cooldown_until > 0:
            self.cooldown_until -= 1
            return

        # Prevent re-entering on the same bar
        if self.last_processed_bar == timestamp and self.trader.position != 0:
            return

        self.has_tp1_hit = False
        stop_loss_pts = self.RISK.get("stop_loss_pts", 60)
        if self.ATR_MULT > 0:
            atr_val = last_5m.get("atr", 0)
            # [Bug fix] ATR 合理性上限：TMF 5m ATR 通常 30-150 點
            atr_cap = 300
            if atr_val > atr_cap:
                atr_val = atr_cap
            if atr_val > 0:
                stop_loss_pts = atr_val * self.ATR_MULT

        # ── 進場品質過濾 ──
        min_score = self.STRATEGY.get("entry_score", 21)
        vol = last_5m.get("Volume", 0)
        avg_vol = df_5m["Volume"].rolling(20).mean().iloc[-1] if len(df_5m) >= 20 else 0

        # 夜盤成交量門檻降低（夜盤 TMF 量通常只有日盤 3-10%）
        hhmm = int(datetime.now().strftime("%H%M"))
        is_night = hhmm >= 1500 or hhmm < 500
        vol_threshold = 0.05 if is_night else 0.3

        vol_filter_ok = (avg_vol == 0) or (vol >= avg_vol * vol_threshold)
        if not vol_filter_ok:
            session_note = "夜盤" if is_night else "日盤"
            console.print(f"[dim]⏸️ Volume too low ({session_note}): {vol:.0f} vs avg {avg_vol:.0f} (>{vol_threshold*100:.0f}%) — skipping entry[/dim]")
            return

        if abs(score) < min_score and self.counter_enabled:
            pass  # Counter mode 有自己的信號系統，不擋
        elif abs(score) < min_score:
            return  # 分數太低，不進場

        # ── Pluggable entry strategy (Elite Strategies) ──
        from strategies.futures.elite_strategies import get_strategy as get_elite_strategy
        from strategies.futures.elite_strategies import select_strategy

        # Always track fires for counter mode
        if self.counter_enabled:
            self._detect_squeeze_failure(last_5m, df_5m)

        # Elite Strategy Auto-Select: 並行嘗試 Spring/Upthrust + Counter-VWAP
        auto_select = self.STRATEGY.get("auto_select", False)
        if auto_select and self.counter_enabled:
            # 1. 先試 Spring/Upthrust (0 延遲, 高品質)
            from strategies.futures.elite_strategies import strategy_spring_upthrust
            spring_signal = strategy_spring_upthrust({
                "last_5m": last_5m, "df_5m": df_5m,
                "score": score, "stop_loss_pts": stop_loss_pts,
            }, self.cfg)
            if spring_signal:
                atr_val = last_5m.get("atr", 0)
                if atr_val > 300: atr_val = 300  # [Bug fix] ATR cap
                spring_sl = spring_signal.get("stop_loss", atr_val * 2.0 if atr_val > 0 else 60)
                lots = self.MGMT.get("lots_per_trade", 1)
                be = self.RISK.get("break_even_pts", 50)
                action = spring_signal["action"]
                if self.MGMT.get(f"allow_{'long' if action == 'BUY' else 'short'}", True):
                    console.print(f"[bold cyan]🌊 SPRING/UPTHRUST {action} SL={spring_sl:.1f}[/bold cyan]")
                    self._execute_trade(action, last_price, timestamp, lots,
                                        stop_loss=spring_sl, break_even_trigger=be, reason=spring_signal["reason"])
                    return  # Spring 進場後不嘗試 Counter
            
            # 2. 再試 Counter-VWAP (盤整市場)
            use_counter = self.counter_enabled and (
                self._is_ranging_regime(df_5m) if self.counter_auto_regime else True
            )
            if use_counter:
                counter_signal = self._detect_squeeze_failure(last_5m, df_5m)
                if counter_signal:
                    atr_val = last_5m.get("atr", 0)
                    if atr_val > 300: atr_val = 300  # [Bug fix] ATR cap
                    counter_sl = atr_val * self.counter_atr_sl_mult if atr_val > 0 else stop_loss_pts
                    lots = self.MGMT.get("lots_per_trade", 1)
                    be = self.RISK.get("break_even_pts", 50)
                    action = "BUY" if counter_signal == "COUNTER_BUY" else "SELL"
                    if self.MGMT.get(f"allow_{'long' if action == 'BUY' else 'short'}", True):
                        console.print(f"[bold magenta]🔄 COUNTER {action} SL={counter_sl:.1f}[/bold magenta]")
                        self._execute_trade(action, last_price, timestamp, lots,
                                            stop_loss=counter_sl, break_even_trigger=be, reason="COUNTER")
                    return
            # 非盤整且無 Spring 信號時 return
            return
        else:
            # Counter mode (auto-regime override)
            use_counter = self.counter_enabled and (
                self._is_ranging_regime(df_5m) if self.counter_auto_regime else True
            )
            if use_counter:
                counter_signal = self._detect_squeeze_failure(last_5m, df_5m)
                if counter_signal:
                    atr_val = last_5m.get("atr", 0)
                    if atr_val > 300: atr_val = 300  # [Bug fix] ATR cap
                    counter_sl = atr_val * self.counter_atr_sl_mult if atr_val > 0 else stop_loss_pts
                    lots = self.MGMT.get("lots_per_trade", 2)
                    be = self.RISK.get("break_even_pts", 50)
                    action = "BUY" if counter_signal == "COUNTER_BUY" else "SELL"
                    if self.MGMT.get(f"allow_{'long' if action == 'BUY' else 'short'}", True):
                        console.print(f"[bold magenta]🔄 COUNTER {action} SL={counter_sl:.1f}[/bold magenta]")
                        self._execute_trade(action, last_price, timestamp, lots,
                                            stop_loss=counter_sl, break_even_trigger=be, reason="COUNTER")
                    return

            # Fallback to config active_strategy
            active = self.STRATEGY.get("active_strategy", "counter_vwap")
            strategy_fn = get_elite_strategy(active)
            if strategy_fn is None:
                # Fallback to old entry_strategies if elite not found
                strategy_fn = get_strategy(active)

        if not strategy_fn or self.trader.position != 0:
            return

        trend = _check_trend_breakout_signal(df_5m, df_15m)
        market_state = {
            "last_5m": last_5m, "last_15m": last_15m, "df_5m": df_5m,
            "score": score, "stop_loss_pts": stop_loss_pts,
            "trend": trend, "hour": datetime.now().hour,
            # Counter-VWAP 需要的狀態
            "fire_pending_dir": getattr(self, '_fire_pending_dir', 0),
            "fire_bar_idx": getattr(self, '_fire_bar_idx', 0),
            "fire_high": getattr(self, '_fire_high', 0.0),
            "fire_low": getattr(self, '_fire_low', 0.0),
            "bar_counter": getattr(self, '_bar_counter', 0),
        }
        signal = strategy_fn(market_state, self.cfg)
        if signal:
            action = signal["action"]
            if self.MGMT.get(f"allow_{'long' if action == 'BUY' else 'short'}", True):
                lots = self.MGMT.get("lots_per_trade", 2)
                be = self.RISK.get("break_even_pts", 50)
                console.print(f"[bold cyan]📌 [{active}] {action} reason={signal['reason']} SL={signal['stop_loss']:.1f}[/bold cyan]")
                self._execute_trade(action, last_price, timestamp, lots,
                                    stop_loss=signal["stop_loss"], break_even_trigger=be,
                                    reason=signal["reason"])
