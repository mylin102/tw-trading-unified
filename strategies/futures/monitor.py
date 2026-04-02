"""
Futures monitor — full strategy from daily_simulation.
Accepts an injected Shioaji API instance (no internal login).
"""
import sys
import os
import time
import yaml
import threading
from datetime import datetime
from collections import deque
from pathlib import Path
import pandas as pd
from rich.console import Console

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from squeeze_futures.engine.constants import get_point_value
from squeeze_futures.engine.simulator import PaperTrader
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment, calculate_atr
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
        self.cooldown_bars = self.STRATEGY.get("cooldown_bars", 3)
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

    def _load_config(self, path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def setup(self):
        if self.dry_run:
            console.print("[yellow][FuturesMonitor] dry-run: skipping contract fetch[/yellow]")
            return True
        self.contract = self.client.get_futures_contract(self.ticker)
        if self.contract is None:
            console.print("[red][FuturesMonitor] contract not found[/red]")
            return False
        console.print(f"[green][FuturesMonitor] contract: {self.contract.code}[/green]")
        # Tick-based bar builder
        self._tick_bars = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        self._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
        # Pre-fill from kbars if available
        try:
            df = self.client.get_kline(self.ticker, interval="5m")
            if not df.empty:
                self._tick_bars = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                console.print(f"[green][FuturesMonitor] pre-filled {len(self._tick_bars)} bars from kbars[/green]")
        except Exception:
            pass
        return True

    def on_tick(self, exchange, tick):
        # Accept tick if it matches contract code OR contract category (TMF)
        if self.contract:
            if tick.code != self.contract.code and not tick.code.startswith("TMF"):
                return
        # Build 5m bars from ticks
        price = float(tick.close)
        vol = int(getattr(tick, "volume", 1))
        ts = pd.Timestamp(tick.datetime).floor("5min")
        bar = self._current_bar
        if bar["ts"] is None or ts > bar["ts"]:
            # Save previous bar
            if bar["ts"] is not None and bar["open"] > 0:
                self._tick_bars.loc[bar["ts"]] = [bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]]
                # Keep last 300 bars (~25 hours)
                if len(self._tick_bars) > 300:
                    self._tick_bars = self._tick_bars.iloc[-300:]
            bar["ts"] = ts
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
                return None
            action = "Sell" if self.trader.position > 0 else "Buy"

        live_ready = self.live_trading and not self.dry_run and self.contract is not None
        if live_ready and action is not None:
            # 進場前檢查保證金（出場不擋）
            if signal in ("BUY", "SELL"):
                if not self._margin_sufficient():
                    console.print(f"[red][FuturesMonitor] ⛔ 保證金不足，取消 {signal}[/red]")
                    return None
            trade = self.client.place_order(self.contract, action=action, quantity=lots)
            if trade is None:
                console.print(f"[red][FuturesMonitor] Live order failed: {signal} {lots}[/red]")
                return None

        # 計算 PnL（出場時）
        pnl_pts = 0
        pnl_cash = 0
        direction = ""
        if signal == "BUY":
            direction = "LONG"
        elif signal == "SELL":
            direction = "SHORT"
        elif signal in ("EXIT", "PARTIAL_EXIT") and self.trader.entry_price > 0:
            direction = "LONG" if self.trader.position > 0 else "SHORT"
            sign = 1 if self.trader.position > 0 else -1
            pnl_pts = (price - self.trader.entry_price) * sign
            pnl_cash = pnl_pts * self.trader.point_value * lots

        save_trade({"type": signal, "timestamp": ts, "price": price, "lots": lots,
                    "direction": direction, "pnl_pts": round(pnl_pts, 1),
                    "pnl_cash": round(pnl_cash, 0), "reason": reason or ""})
        if signal in ("BUY", "SELL"):
            self._last_entry_reason = reason
        result = self.trader.execute_signal(
            signal, price, ts, lots=lots,
            max_lots=self.MGMT.get("max_positions", 2),
            stop_loss=stop_loss, break_even_trigger=break_even_trigger, exit_reason=reason,
        )
        if result:
            d = "🟢 BUY" if signal == "BUY" else "🔴 SELL" if signal == "SELL" else "⚪ EXIT"
            console.print(f"[bold green][FuturesMonitor] [{ts}] {d} {lots} lots @ {price:.0f}  {result}[/bold green]")
            if live_ready and send_email_notification:
                send_email_notification(
                    f"[TMF] {signal} {lots} lots @ {price:.0f}",
                    f"{d} {lots} lots @ {price:.0f}\n{result}",
                )
        return result

    def _check_stop_loss(self, ts, price):
        if self.trader.position == 0:
            return None
            
        sl_dist = self.RISK.get("stop_loss_pts", 60)
        # 如果有設定 ATR 倍數，則使用動態停損
        if self.ATR_MULT > 0:
            # 這裡需要傳入當前的 df_5m 來算最新的 ATR
            # 但為了效率，我們可以假設在 _strategy_tick 中已經算好了，或者這裡重新算
            # 這裡簡單處理：如果 trader 有 current_stop_loss 就用它
            pass

        if self.trader.position > 0 and self.trader.current_stop_loss and price <= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", self.trader.current_stop_loss, ts, abs(self.trader.position), reason="STOP_LOSS")
        if self.trader.position < 0 and self.trader.current_stop_loss and price >= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", self.trader.current_stop_loss, ts, abs(self.trader.position), reason="STOP_LOSS")
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
        
        # 修正：支援交易日邏輯，凌晨 5 點前算在前一天
        now = datetime.now()
        date_str = (now - datetime.timedelta(days=1)).strftime('%Y%m%d') if now.hour < 5 else now.strftime('%Y%m%d')
        
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
                console.print(f"[red][FuturesMonitor] error: {e}[/red]")
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
            # 優先使用內部的 tick_bars (如果已經累積足夠)
            if hasattr(self, "_tick_bars") and len(self._tick_bars) >= 100:
                df_base = self._tick_bars.copy()
                processed["5m"] = calculate_futures_squeeze(df_base, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                
                # Resample for higher timeframes
                for tf, rule in [("15m", "15min"), ("1h", "1h")]:
                    res = df_base.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
                    if len(res) >= 20:
                        processed[tf] = calculate_futures_squeeze(res, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
            
            # 如果數據不足，才去調用 API (且限制頻率)
            if "5m" not in processed:
                try:
                    if self.api and hasattr(self.api, "kbars"):
                        df = self.client.get_kline(self.ticker, interval="5m")
                        if not df.empty:
                            processed["5m"] = calculate_futures_squeeze(df, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                except Exception as e:
                    console.print(f"[yellow][FuturesMonitor] api.kbars failed: {e}[/yellow]")

        # Fallback: use tick-built bars if kbars API returns empty
        if "5m" not in processed and hasattr(self, "_tick_bars") and len(self._tick_bars) >= 30:
            df_tick = self._tick_bars.copy()
            processed["5m"] = calculate_futures_squeeze(df_tick, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
            if "15m" not in processed:
                r15 = df_tick.resample("15min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
                if len(r15) >= 20:
                    processed["15m"] = calculate_futures_squeeze(r15, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
            if "1h" not in processed:
                r1h = df_tick.resample("1h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
                if len(r1h) >= 20:
                    processed["1h"] = calculate_futures_squeeze(r1h, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)

        if "5m" not in processed or "15m" not in processed:
            return

        df_5m, df_15m = processed["5m"], processed["15m"]
        last_5m, last_15m = df_5m.iloc[-1], df_15m.iloc[-1]
        score = calculate_mtf_alignment(processed, weights=self.STRATEGY.get("weights", {"5m": 0.4, "15m": 0.4, "1h": 0.2}))["score"]
        last_price = last_5m["Close"]
        vwap = last_5m["vwap"]
        timestamp = last_5m.name

        # Log bar
        if self.last_processed_bar != timestamp:
            regime = "STRONG" if last_5m.get("opening_bullish") else ("WEAK" if last_5m.get("opening_bearish") else "NORMAL")
            self._save_bar(last_5m, score, regime)
            self.last_processed_bar = timestamp
            self._bar_counter += 1
            console.print(f"[dim][FuturesMonitor] {datetime.now().strftime('%H:%M')} close={last_price:.0f} score={score:.1f}[/dim]")

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
                if vwap_exit and ((self.trader.position > 0 and last_price < vwap) or (self.trader.position < 0 and last_price > vwap)):
                    stop_msg = self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="VWAP")
            if stop_msg:
                self.has_tp1_hit = False
                self.cooldown_until = self.cooldown_bars # 觸發停損/平倉後進入冷卻
            return  # don't enter same bar as exit

        # 3. Entry logic (with cooldown check)
        if self.cooldown_until > 0:
            self.cooldown_until -= 1
            return

        self.has_tp1_hit = False
        stop_loss_pts = self.RISK.get("stop_loss_pts", 60)
        if self.ATR_MULT > 0:
            atr_val = last_5m.get("atr", 0)
            if atr_val > 0:
                stop_loss_pts = atr_val * self.ATR_MULT

        # Determine active mode: counter (mean-reversion) vs breakout
        use_counter = False
        if self.counter_enabled:
            if self.counter_auto_regime:
                use_counter = self._is_ranging_regime(df_5m)
            else:
                use_counter = True

        if use_counter:
            # ── Squeeze Failure Counter entry ──
            counter_signal = self._detect_squeeze_failure(last_5m, df_5m)
            if counter_signal:
                atr_val = last_5m.get("atr", 0)
                counter_sl = atr_val * self.counter_atr_sl_mult if atr_val > 0 else stop_loss_pts
                lots = self.MGMT.get("lots_per_trade", 2)
                be = self.RISK.get("break_even_pts", 50)
                if counter_signal == "COUNTER_BUY" and self.MGMT.get("allow_long", True):
                    console.print(f"[bold magenta]🔄 COUNTER BUY (failure reversal) SL={counter_sl:.1f}[/bold magenta]")
                    self._execute_trade("BUY", last_price, timestamp, lots,
                                        stop_loss=counter_sl, break_even_trigger=be, reason="COUNTER")
                elif counter_signal == "COUNTER_SELL" and self.MGMT.get("allow_short", True):
                    console.print(f"[bold magenta]🔄 COUNTER SELL (failure reversal) SL={counter_sl:.1f}[/bold magenta]")
                    self._execute_trade("SELL", last_price, timestamp, lots,
                                        stop_loss=counter_sl, break_even_trigger=be, reason="COUNTER")
        else:
            # ── Original Breakout entry ──
            # Still track fires for counter even when not active (seamless switch)
            if self.counter_enabled:
                self._detect_squeeze_failure(last_5m, df_5m)

            entry_score = self.STRATEGY.get("entry_score", 20)
            sqz_buy = (not last_5m["sqz_on"]) and score >= entry_score and last_5m["mom_state"] >= 2
            sqz_sell = (not last_5m["sqz_on"]) and score <= -entry_score and last_5m["mom_state"] <= 1

            # Regime filter
            if self.FILTER_MODE == "loose":
                can_long = can_short = True
            elif self.FILTER_MODE == "mid":
                can_long = last_15m["Close"] > last_15m["ema_filter"] * 0.998
                can_short = last_15m["Close"] < last_15m["ema_filter"] * 1.002
                if last_5m.get("bullish_align", False):
                    can_short = False
                if last_5m.get("bearish_align", False):
                    can_long = False
            else:
                can_long = last_15m["Close"] > last_15m["ema_filter"] * 0.999
                can_short = last_15m["Close"] < last_15m["ema_filter"] * 1.001

            trend = _check_trend_breakout_signal(df_5m, df_15m)
            lots = self.MGMT.get("lots_per_trade", 2)
            be = self.RISK.get("break_even_pts", 50)

            entry_reason = ""
            if sqz_buy and can_long and trend["trend_long"]: entry_reason = "SYNERGY"
            elif sqz_buy and can_long: entry_reason = "SQUEEZE"
            elif trend["trend_long"]: entry_reason = "BREAKOUT"
            
            if entry_reason and self.MGMT.get("allow_long", True):
                self._execute_trade("BUY", last_price, timestamp, lots, stop_loss=stop_loss_pts, break_even_trigger=be, reason=entry_reason)
            else:
                if sqz_sell and can_short and trend["trend_short"]: entry_reason = "SYNERGY"
                elif sqz_sell and can_short: entry_reason = "SQUEEZE"
                elif trend["trend_short"]: entry_reason = "BREAKOUT"
                
                if entry_reason and self.MGMT.get("allow_short", True):
                    self._execute_trade("SELL", last_price, timestamp, lots, stop_loss=stop_loss_pts, break_even_trigger=be, reason=entry_reason)
