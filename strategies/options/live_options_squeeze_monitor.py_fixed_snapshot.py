    def record_signal_snapshot(self, signal):
        # 即使沒有訊號也用即時報價算 Greeks
        iv, delta_val, gamma_val, vega_val = 0.0, 0.0, 0.0, 0.0
        # Fix: initialize strike/dte_years outside try block to prevent NameError
        strike = 0.0
        dte_years = 3.0 / 365.0
        price_mtx = float(self.market_data["MTX"]["close"])
        
        # 優先使用最新計算出的分數與趨勢，若 signal 存在則覆蓋
        score = float(signal["score"]) if signal else self.latest_score
        mid_trend = (signal["mid_trend"] or "") if signal else self.latest_mid_trend
        side_label = (signal["side"] or "") if signal else ""

        if price_mtx <= 0:
            # 💡 GSD: Don't record if price is 0 (initialization spike)
            return

        try:
            calc_side = (signal.get("side") if signal and signal.get("side") else "C")
            quote = self.current_option_quote(calc_side)
            contract = self.active_contracts.get(calc_side)
            strike = float(getattr(contract, "strike_price", resolve_option_strike(price_mtx, self.strike_rounding)))
            delivery_date = getattr(contract, "delivery_date", None)
            dte_years = float(self._dte(delivery_date) if delivery_date else 3.0 / 365.0)
            option_price = float(quote["mid"])
            option_type = 'c' if calc_side == 'C' else 'p'

            if option_price > 0 and strike > 0:
                try:
                    iv = float(self._iv(option_price, price_mtx, strike, dte_years, self.risk_free_rate, option_type))
                    res = self._bs(price_mtx, strike, dte_years, self.risk_free_rate, iv, option_type=calc_side)
                    delta_val, gamma_val, vega_val = res["delta"], res["gamma"], res["vega"]
                except Exception:
                    res = self._bs(price_mtx, strike, dte_years, self.risk_free_rate, 0.25, option_type=calc_side)
                    iv, delta_val, gamma_val, vega_val = 0.25, res["delta"], res["gamma"], res["vega"]
            elif strike > 0:
                res = self._bs(price_mtx, strike, dte_years, self.risk_free_rate, 0.25, option_type=calc_side)
                iv, delta_val, gamma_val, vega_val = 0.25, res["delta"], res["gamma"], res["vega"]
        except Exception as e:
            console.print(f"[red]Greeks calculation error:[/red] {e}")

        from core.date_utils import get_session
        now = datetime.datetime.now()
        
        # GSD: Base on signal data to ensure all indicators are preserved
        row = signal.copy() if signal else {}
        
        # Standardize and add Greeks
        row.update({
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "session": get_session(now),
            "score": score,
            "side": side_label,
            "price_mtx": price_mtx,
            "strike": strike,
            "dte": round(dte_years * 365, 2),
            "mid_trend": mid_trend,
            "iv": round(iv, 4),
            "delta": round(delta_val, 4),
            "gamma": round(gamma_val, 6),
            "vega": round(vega_val, 4),
            # Backwards compatibility/aliases for dashboard
            "vwap": price_mtx,
            "sqz_on": row.get("sqz_on", row.get("squeeze_on", False)),
        })
        
        if iv > 0:
            self.latest_iv = iv
            
        # GSD Fix: Support dynamic column expansion
        df_row = pd.DataFrame([row])
        if self.indicator_log_path.exists():
            try:
                df_existing = pd.read_csv(self.indicator_log_path)
                df_combined = pd.concat([df_existing, df_row], ignore_index=True)
                # For options, we might have multiple signals per minute if it refreshes frequently
                # but usually it's once per minute or per tick. Let's keep last by timestamp.
                df_combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
                df_combined.to_csv(self.indicator_log_path, index=False)
            except Exception:
                df_row.to_csv(self.indicator_log_path, mode="a", index=False, header=False)
        else:
            df_row.to_csv(self.indicator_log_path, index=False, header=True)
