import pandas as pd
import numpy as np

try:
    import pandas_ta as ta  # noqa: F401
except Exception:
    ta = None


from core.date_utils import get_trading_day

def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    # 確保欄位標準化 (V-Model 修正)
    temp_df = df.copy()
    temp_df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in temp_df.columns]
    temp_df = temp_df.loc[:, ~temp_df.columns.duplicated()]
    
    high = temp_df.get("High")
    low = temp_df.get("Low")
    close = temp_df.get("Close")
    
    # 如果缺少必要欄位，回傳 0 系列而非崩潰 (V-Model 容錯)
    if high is None or low is None or close is None:
        return pd.Series(0.0, index=df.index)

    close_prev = close.shift(1)
    return pd.concat(
        [
            high - low,
            (high - close_prev).abs(),
            (low - close_prev).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _linreg(series: pd.Series, length: int) -> pd.Series:
    """計算線性回歸值 (用於動能平滑)"""
    x = np.arange(length)
    def get_linreg(y):
        if len(y) < length or np.isnan(y).any():
            return 0.0
        slope, intercept = np.polyfit(x, y, 1)
        return slope * (length - 1) + intercept
    return series.rolling(window=length).apply(get_linreg, raw=True)


def _fallback_squeeze(
    df: pd.DataFrame,
    bb_length: int,
    bb_std: float,
    kc_length: int,
    kc_scalar: float,
) -> tuple[pd.Series, pd.Series]:
    """
    實作 TTM Squeeze 的經典動能算法 (V-Model 專業版)
    算法：Linear Regression of Price relative to (SMA + Donchian Mid)/2
    """
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    # 1. Bollinger Bands
    basis = close.rolling(window=bb_length).mean()
    deviation = close.rolling(window=bb_length).std(ddof=0)
    bb_upper = basis + deviation * bb_std
    bb_lower = basis - deviation * bb_std

    # 2. Keltner Channels
    ema_basis = _ema(close, kc_length)
    atr = _true_range(df).rolling(window=kc_length).mean()
    kc_upper = ema_basis + atr * kc_scalar
    kc_lower = ema_basis - atr * kc_scalar

    # 3. Squeeze 狀態
    sqz_on = (bb_lower >= kc_lower) & (bb_upper <= kc_upper)

    # 4. 動能直方圖 (Momentum Histogram)
    # 基準 = (20均線 + 20日高低中點) / 2
    donchian_mid = (high.rolling(window=bb_length).max() + low.rolling(window=bb_length).min()) / 2
    combined_basis = (basis + donchian_mid) / 2
    
    # 偏離值 = 價格 - 基準
    raw_momentum = close - combined_basis
    
    # 對偏離值做線性回歸平滑 (經典 TTM 做法)
    momentum = _linreg(raw_momentum, bb_length)

    return sqz_on.fillna(False), momentum.fillna(0.0)


def _pandas_ta_squeeze(
    df: pd.DataFrame,
    bb_length: int,
    bb_std: float,
    kc_length: int,
    kc_scalar: float,
) -> tuple[pd.Series, pd.Series]:
    if ta is None:
        raise RuntimeError("pandas_ta unavailable")

    sqz = df.ta.squeeze(
        bb_length=bb_length,
        bb_std=bb_std,
        kc_length=kc_length,
        kc_scalar=kc_scalar,
        lazy=True,
    )

    sqz_on_cols = [c for c in sqz.columns if "SQZ_ON" in c]
    mom_cols = [
        c for c in sqz.columns if "SQZ_" in c and not any(x in c for x in ["ON", "OFF", "NO"])
    ]
    sqz_on = sqz[sqz_on_cols[0]].astype(bool) if sqz_on_cols else pd.Series(False, index=df.index)
    momentum = sqz[mom_cols[0]].fillna(0.0) if mom_cols else pd.Series(0.0, index=df.index)
    return sqz_on, momentum


def calculate_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """計算 ATR (Average True Range)"""
    if df.empty or len(df) < length:
        return pd.Series(0.0, index=df.index)
    return _true_range(df).rolling(window=length, min_periods=length).mean()


def calculate_futures_squeeze(
    df: pd.DataFrame,
    bb_length: int = 20,
    bb_std: float = 2.0,
    kc_length: int = 20,
    kc_scalar: float = 2.0,  # V-Model 波動率適應性修正 (1.5 -> 2.0)
    pb_buffer: float = 1.002,
    ema_fast: int = 12,
    ema_slow: int = 36,
    ema_macro: int = 200,
    breakout_lookback: int = 10,  # [GSD] 從 20 縮短至 10，提升反應速度
) -> pd.DataFrame:
    """
    計算完整的期貨 Squeeze + 趨勢指標
    """
    # 1. 確保欄位大小寫標準化，並處理重複欄位 (V-Model 修正)
    df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
    # 如果同時存在 'close' 和 'Close'，capitalize 後會重複，取第一筆即可
    df = df.loc[:, ~df.columns.duplicated()].copy()

    # 2. 確保有 DatetimeIndex 才能進行夜盤換日運算
    if not isinstance(df.index, pd.DatetimeIndex):
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.set_index("timestamp")
        else:
            # 如果完全沒有時間資訊，給予預設日期以便運算不崩潰
            df.index = pd.date_range("2026-01-01", periods=len(df), freq="5min")

    # ── GSD: Always calculate trading_day first (essential for logs/dashboard) ──
    df = df.copy()
    df["trading_day"] = get_trading_day(df.index)

    if len(df) < bb_length:
        # GSD: Ensure required columns exist for short dataframes
        # This list MUST match the full calculation to avoid concat issues
        for col in ["sqz_on", "momentum", "atr", "mom_velo", "vwap", "score", "regime", 
                    "bull_align", "bear_align", "bullish_align", "bearish_align", "in_pb_zone",
                    "ema_fast", "ema_slow", "ema_filter", "ema_macro", "fired",
                    "squeeze_release", "bull_breakout", "bear_breakout", "bb_up", "bb_low",
                    "day_open", "day_min", "day_max",
                    "opening_bullish", "opening_bearish",
                    "adx", "breakout_strength", "bear_breakout_strength",
                    "breakout_strength_atr", "price_vs_vwap",
                    "trend_strength_raw", "volume_spike",
                    "is_structural_breakout",
                    "atr_raw", "atr_floor", "atr_used",
                    "mom_state", "mom_prev"]:
            if col not in df.columns:
                if col in ["sqz_on", "fired", "squeeze_release", "bull_breakout", "bear_breakout",
                           "bull_align", "bear_align", "bullish_align", "bearish_align", "in_pb_zone",
                           "opening_bullish", "opening_bearish"]:
                    df[col] = False
                elif col == "regime":
                    df[col] = "NORMAL"
                elif col == "score":
                    df[col] = 0.0
                elif col in ("day_open", "day_min", "day_max"):
                    # df may be empty; use Close as fallback
                    df[col] = df["Close"].iloc[0] if len(df) > 0 else 0.0
                elif col == "vwap":
                    df[col] = df["Close"] if len(df) > 0 else np.nan
                else:
                    df[col] = 0.0
        return df

    if ta:
        sqz_on, momentum = _pandas_ta_squeeze(df, bb_length, bb_std, kc_length, kc_scalar)
    else:
        sqz_on, momentum = _fallback_squeeze(df, bb_length, bb_std, kc_length, kc_scalar)

    res = df.copy()
    res["sqz_on"] = sqz_on
    res["momentum"] = momentum
    res["atr"] = calculate_atr(df, length=bb_length)
    
    # 計算動能斜率 (Velocity): 3 棒變化量的移動平均
    res["mom_velo"] = res["momentum"].diff(1).rolling(window=3).mean().fillna(0.0)
    
    # 改進 VWAP：使用交易日 (Trading Day) 而非日曆日 (Calendar Day)
    # 台指期規則：15:00 以後屬下一個交易日
    res["trading_day"] = get_trading_day(res.index)
    
    if "Close" in res.columns and "Volume" in res.columns:
        typical_price_x_volume = res["Close"] * res["Volume"]
        volume_cumsum = res.groupby("trading_day")["Volume"].cumsum()
        res["vwap"] = typical_price_x_volume.groupby(res["trading_day"]).cumsum() / volume_cumsum
        res["vwap"] = res["vwap"].where(volume_cumsum != 0, res["Close"])
        res["price_vs_vwap"] = np.where(res["vwap"] != 0, (res["Close"] - res["vwap"]) / res["vwap"], 0.0)

    # ═══ [Live Volume Spike] 即時計算，不依賴 Parquet/CSV 舊欄位 ═══
    # 使用 20 期成交量移動平均作為基準
    _vol_ma20 = res["Volume"].rolling(20, min_periods=5).mean().ffill().fillna(res["Volume"])
    res["volume_spike"] = (res["Volume"] / _vol_ma20.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    # Debug log — 只在第一次執行時打，避免刷屏
    if not getattr(calculate_futures_squeeze, "_vol_spike_logged", False):
        _last = res.iloc[-1] if len(res) > 0 else None
        if _last is not None:
            print(
                f"[LiveVolumeSpike] vol={_last.get('Volume', '?'):_} "
                f"vol_ma20={_vol_ma20.iloc[-1]:_.0f} "
                f"spike={_last.get('volume_spike', '?'):.2f} "
                f"source=live_calculated",
                flush=True,
            )
        calculate_futures_squeeze._vol_spike_logged = True
    
    res["fired"] = (~res["sqz_on"]) & (res["sqz_on"].shift(1))

    # [Wave 4 Fix] Add Bollinger Bands and RSI for Mean Reversion
    # 1. Bollinger Bands
    basis = res["Close"].rolling(window=bb_length).mean()
    deviation = res["Close"].rolling(window=bb_length).std(ddof=0)
    res["bb_up"] = basis + deviation * bb_std
    res["bb_mid"] = basis
    res["bb_low"] = basis - deviation * bb_std

    # ── [Night Fix] 方向性突破：squeeze 中脫離 BB ──
    # squeeze_release: BB 不再被 KC 包住（volatility state transition）
    res["squeeze_release"] = res["fired"]
    # bull_breakout: 在 squeeze 中 Close 突破 BB 上緣
    # breakout_strength 可能尚未存在（例如 BreakoutEngineV2 拋 exception 時）
    if "breakout_strength" in res.columns and "bear_breakout_strength" in res.columns:
        res["bull_breakout"] = (
            res["sqz_on"]
            & (res["Close"] > res["bb_up"])
            & (res["breakout_strength"] > 0)
        )
        res["bear_breakout"] = (
            res["sqz_on"]
            & (res["Close"] < res["bb_low"])
            & (res["bear_breakout_strength"] > 0)
        )
    else:
        res["bull_breakout"] = False
        res["bear_breakout"] = False

    # 2. RSI
    try:
        if ta:
            res["rsi"] = res.ta.rsi(length=14)
        else:
            delta = res["Close"].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            res["rsi"] = 100 - (100 / (1 + rs))
    except Exception:
        res["rsi"] = 50.0

    # 3. ADX (Trend Strength)
    try:
        if ta:
            adx_df = res.ta.adx(length=14)
            if adx_df is not None:
                # pandas_ta ADX column name is usually ADX_14
                adx_col = [c for c in adx_df.columns if "ADX" in c][0]
                res["adx"] = adx_df[adx_col]
            else:
                res["adx"] = 20.0
        else:
            res["adx"] = 20.0
    except Exception:
        res["adx"] = 20.0

    # 向量化 mom_state 計算，提升效能
    res["mom_prev"] = res["momentum"].shift(1).fillna(0)
    m = res["momentum"].values
    p = res["mom_prev"].values
    
    res["mom_state"] = np.select(
        [
            (m > 0) & (m >= p),
            (m > 0) & (m < p),
            (m <= 0) & (m <= p),
            (m <= 0) & (m > p)
        ],
        [3, 2, 0, 1],
        default=1
    )

    # 趨勢與過濾器
    res["ema_fast"] = _ema(res["Close"], ema_fast)
    res["ema_slow"] = _ema(res["Close"], ema_slow)
    res["ema_filter"] = _ema(res["Close"], 60)
    res["ema_macro"] = _ema(res["Close"], ema_macro) # 預設為 200
    res["bullish_align"] = res["ema_fast"] > res["ema_slow"]
    res["bearish_align"] = res["ema_fast"] < res["ema_slow"]
    
    # 200日趨勢判斷
    res["ema_200_up"] = (res["Close"] > res["ema_macro"]) & (res["ema_macro"] > res["ema_macro"].shift(1))

    # --- 新增：MACD 計算 ---
    ema12 = _ema(res["Close"], 12)
    ema26 = _ema(res["Close"], 26)
    res["macd_line"] = ema12 - ema26
    res["macd_signal"] = _ema(res["macd_line"], 9)
    res["macd_hist"] = res["macd_line"] - res["macd_signal"]
    # 動能增強判斷
    res["macd_rising"] = res["macd_hist"] > res["macd_hist"].shift(1)

    # --- 新增：KD (Stochastic) 計算 ---
    k_period = 9
    d_period = 3
    if "Low" in res.columns and "High" in res.columns:
        low_min = res["Low"].rolling(window=k_period).min()
        high_max = res["High"].rolling(window=k_period).max()
        res["rsv"] = (res["Close"] - low_min) / (high_max - low_min) * 100
        res["k_val"] = res["rsv"].ewm(com=2, adjust=False).mean()
        res["d_val"] = res["k_val"].ewm(com=2, adjust=False).mean()
    else:
        res["rsv"] = 50.0
        res["k_val"] = 50.0
        res["d_val"] = 50.0
    
    # Bollinger Bands
    bb_basis = res["Close"].rolling(window=bb_length).mean()
    bb_dev = res["Close"].rolling(window=bb_length).std()
    res["bb_upper"] = bb_basis + bb_std * bb_dev
    res["bb_lower"] = bb_basis - bb_std * bb_dev

    # --- Single-timeframe score (for single-test backtest without MTF) ---
    # Score: normalized momentum + squeeze state + EMA alignment
    # Range: -100 to +100 (approximate)
    if "mom_state" in res.columns:
        # mom_state is 0-4, normalize to -100 to +100
        mom_score = (res["mom_state"] - 1.5) / 1.5 * 100
    else:
        mom_score = res["momentum"] / max(res["momentum"].abs().max(), 1) * 100

    ema_bias = pd.Series(0.0, index=res.index)
    if "bullish_align" in res.columns:
        ema_bias = ema_bias + res["bullish_align"].astype(float) * 50
    if "bearish_align" in res.columns:
        ema_bias = ema_bias - res["bearish_align"].astype(float) * 50

    res["score"] = mom_score * 0.7 + ema_bias * 0.3

    # 波動率調整後的 Pullback 區域
    if "High" in res.columns and "Low" in res.columns:
        res["recent_high"] = res["High"].rolling(window=bb_length).max()
        res["recent_low"] = res["Low"].rolling(window=bb_length).min()
        res["is_new_high"] = res["Close"] >= res["recent_high"].shift(1)
        res["is_new_low"] = res["Close"] <= res["recent_low"].shift(1)
    
    res["in_bull_pb_zone"] = (
        (res["Close"] <= res["ema_fast"] * pb_buffer)
        & (res["Close"] >= res["ema_slow"])
        & res["bullish_align"]
    )
    res["in_bear_pb_zone"] = (
        (res["Close"] >= res["ema_fast"] * (2 - pb_buffer))
        & (res["Close"] <= res["ema_slow"])
        & res["bearish_align"]
    )

    # 開盤型態 (需要 Open 欄位)
    if "Open" in res.columns:
        res["day_open"] = res.groupby("trading_day")["Open"].transform("first")
        res["day_min"] = res.groupby("trading_day")["Low"].cummin()
        res["day_max"] = res.groupby("trading_day")["High"].cummax()
        res["opening_bullish"] = (res["Close"] > res["day_open"]) & (res["day_min"] >= res["day_open"] * 0.999)
        res["opening_bearish"] = (res["Close"] < res["day_open"]) & (res["day_max"] <= res["day_open"] * 1.001)

    # 趨勢強度指標 (ADX) - V-Model 震盪避讓修正
    if "High" in res.columns and "Low" in res.columns and "Close" in res.columns:
        if ta:
            adx_df = res.ta.adx(length=14)
            res["adx"] = adx_df["ADX_14"]
        else:
            res["adx"] = 25.0
    else:
        res["adx"] = 0.0

    # [V-Model Upgrade] Breakout Engine V2: High-fidelity metric separation & ATR Trace
    if len(res) < 5 or "Close" not in res.columns:
        for col in [
            "breakout_strength_atr", "intraday_strength_pct", "price_vs_vwap_pct", 
            "is_bull_structural_breakout", "is_bear_structural_breakout",
            "atr_raw", "atr_floor", "atr_used"
        ]:
            res[col] = 0.0
        res["trend_strength_raw"] = 0.0
        return res

    try:
        # 1. 準備基礎數據
        _atr = calculate_atr(res, length=14)
        _high_n = res["High"].rolling(window=breakout_lookback).max().shift(1)
        _low_n = res["Low"].rolling(window=breakout_lookback).min().shift(1)
        _close = res["Close"]
        _open = res["day_open"] if "day_open" in res.columns else _close
        _vwap = res["vwap"] if "vwap" in res.columns else _close
        
        # 2. ATR Traceability (V-Model requirement)
        res['atr_raw'] = _atr.fillna(0.0)
        res['atr_floor'] = (_close * 0.0015).fillna(0.0)
        res['high_20_prev'] = _high_n.fillna(0.0)  # 名稱保留為 20 以相容 Dashboard，但實際週期由 breakout_lookback (10) 決定
        res['low_20_prev'] = _low_n.fillna(0.0)
        res['atr_used'] = res[['atr_raw', 'atr_floor']].max(axis=1).replace(0, np.nan).fillna(50.0)
        
        # 3. 原始百分比與偏離度 (Traceability)
        res["intraday_strength_pct"] = ((_close - _open) / _open * 100.0).fillna(0.0)
        res["price_vs_vwap_pct"] = ((_close - _vwap) / _vwap * 100.0).fillna(0.0)
        
        # 4. 結構突破判定 (Separated Bull/Bear)
        res["is_bull_structural_breakout"] = (_close > _high_n).astype(int)
        res["is_bear_structural_breakout"] = (_close < _low_n).astype(int)
        
        # 5. ATR 正規化突破強度 (使用 atr_used)
        res["breakout_strength_atr"] = ((_close - _high_n) / res["atr_used"]).clip(lower=0.0).fillna(0.0)
        res["bear_breakout_strength_atr"] = ((_low_n - _close) / res["atr_used"]).clip(lower=0.0).fillna(0.0)
        
        # 相容舊版欄位名 (Router 使用)
        res["breakout_strength"] = res["breakout_strength_atr"]
        res["bear_breakout_strength"] = res["bear_breakout_strength_atr"]
        res["is_structural_breakout"] = res["is_bull_structural_breakout"] - res["is_bear_structural_breakout"]
        
        if not getattr(calculate_futures_squeeze, "_debug_breakout_v2_trace_once", False):
            print(
                f"[BreakoutV2] Trace active. "
                f"ATR_Raw={res['atr_raw'].iloc[-1]:.2f}, Floor={res['atr_floor'].iloc[-1]:.2f}, "
                f"Used={res['atr_used'].iloc[-1]:.2f}, BS={res['breakout_strength_atr'].iloc[-1]:.4f}",
                flush=True
            )
            calculate_futures_squeeze._debug_breakout_v2_trace_once = True
            
    except Exception as e:
        if not getattr(calculate_futures_squeeze, "_warned_breakout_v2", False):
            print(f"[Indicators] BreakoutEngineV2 failed: {e}", flush=True)
            calculate_futures_squeeze._warned_breakout_v2 = True
        for col in ["breakout_strength", "bear_breakout_strength", "breakout_strength_atr", "is_bull_structural_breakout", "is_bear_structural_breakout"]:
            res[col] = 0.0
    try:
        if len(res) >= 2 and "ema_fast" in res.columns:
            _ef = pd.to_numeric(res["ema_fast"], errors="coerce") if "ema_fast" in res.columns else pd.Series(np.nan, index=res.index)
            _ep = _ef.shift(1).replace(0, np.nan)
            _es = ((_ef - _ep) / _ep).replace([np.inf, -np.inf], np.nan).fillna(0)
            res["trend_strength_raw"] = pd.Series(_es, index=res.index).fillna(0) * 100.0
        else:
            res["trend_strength_raw"] = 0.0
    except Exception as e:
        if not getattr(calculate_futures_squeeze, "_warned_trend_strength", False):
            print(f"[Indicators] trend_strength_raw failed: {e}", flush=True)
            calculate_futures_squeeze._warned_trend_strength = True
        res["trend_strength_raw"] = 0.0

    return res


def calculate_mtf_alignment(processed_dfs: dict[str, pd.DataFrame], weights: dict[str, float]) -> dict:
    """
    計算多週期對齊分數
    """
    if not processed_dfs:
        return {"score": 0}

    latest_states = {}
    for tf, df in processed_dfs.items():
        if not df.empty:
            # 使用 mom_state (0-3) 作為基準，轉換為 -1.5 到 +1.5
            if "mom_state" in df.columns:
                val = df["mom_state"].iloc[-1]
                latest_states[tf] = val - 1.5
            else:
                # 如果沒有 mom_state，使用預設值
                latest_states[tf] = 0

    total_score = 0
    available_weight = 0
    for tf, val in latest_states.items():
        w = weights.get(tf, 0.1)
        total_score += val * w
        available_weight += w
    return {"score": (total_score / (1.5 * available_weight)) * 100 if available_weight > 0 else 0}
