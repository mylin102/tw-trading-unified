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
            "lookback": self.PB.get("lookback", 60),
            "pb_buffer": self.PB.get("buffer", 1.002),
        }
        self.live_trading = self.cfg.get("live_trading", False)
        self.cooldown_bars = self.STRATEGY.get("cooldown_bars", 3)
        self.cooldown_until = 0

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
        if self.trader.position > 0 and self.trader.current_stop_loss and price <= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", self.trader.current_stop_loss, ts, abs(self.trader.position), reason="STOP_LOSS")
        if self.trader.position < 0 and self.trader.current_stop_loss and price >= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", self.trader.current_stop_loss, ts, abs(self.trader.position), reason="STOP_LOSS")
        return None

    def _save_bar(self, row, score, regime):
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs", "market_data")
        os.makedirs(log_dir, exist_ok=True)
        
        # 修正：支援交易日邏輯，凌晨 5 點前算在前一天
        now = datetime.now()
        date_str = (now - datetime.timedelta(days=1)).strftime('%Y%m%d') if now.hour < 5 else now.strftime('%Y%m%d')
        
        tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
        path = os.path.join(log_dir, f"{self.ticker}_{date_str}{tag}_indicators.csv")
        
        data = {
            "timestamp": [row.name], 
            "open": [row["Open"]], "high": [row["High"]], "low": [row["Low"]], "close": [row["Close"]], 
            "vwap": [row["vwap"]], "score": [score],
            "sqz_on": [row["sqz_on"]], "mom_state": [row["mom_state"]], "regime": [regime],
            "bull_align": [row["bullish_align"]], "bear_align": [row["bearish_align"]],
            "in_pb_zone": [row.get("in_bull_pb_zone", False) or row.get("in_bear_pb_zone", False)],
        }
        header = not os.path.exists(path)
        pd.DataFrame(data).to_csv(path, mode="a", index=False, header=header)

    # ── Main strategy loop ──
    def run(self):
        self._running = True
        mode = "dry-run" if self.dry_run else ("LIVE" if self.live_trading else "PAPER")
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
            if not stop_msg and self.RISK.get("exit_on_vwap"):
                if (self.trader.position > 0 and last_price < vwap) or (self.trader.position < 0 and last_price > vwap):
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
            atr_s = calculate_atr(df_5m, length=self.ATR_LENGTH)
            if not atr_s.empty and not pd.isna(atr_s.iloc[-1]):
                stop_loss_pts = atr_s.iloc[-1] * self.ATR_MULT

        entry_score = self.STRATEGY.get("entry_score", 20)
        sqz_buy = (not last_5m["sqz_on"]) and score >= entry_score and last_5m["mom_state"] >= 2
        sqz_sell = (not last_5m["sqz_on"]) and score <= -entry_score and last_5m["mom_state"] <= 1

        # Regime filter
        if self.FILTER_MODE == "loose":
            can_long = can_short = True
        elif self.FILTER_MODE == "mid":
            can_long = last_15m["Close"] > last_15m["ema_filter"] * 0.998
            can_short = last_15m["Close"] < last_15m["ema_filter"] * 1.002
            # bull_align guard: 多頭排列時禁止做空，空頭排列時禁止做多
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

        # 決定進場原因
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
