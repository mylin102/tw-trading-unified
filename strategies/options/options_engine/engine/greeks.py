import numpy as np
from scipy.stats import norm
from datetime import datetime

# Black-Scholes 核心公式 (升級版)
def black_scholes(S, K, T, r, sigma, option_type='C'):
    """
    S: 標的物價格, K: 履約價, T: 到期時間(年), r: 無風險利率, sigma: 隱含波動率
    返回: dict 包含 price, delta, gamma, theta, vega
    """
    # 強制轉為 float，避免 decimal.Decimal 導致計算錯誤
    S, K, T, r, sigma = float(S), float(K), float(T), float(r), float(sigma)
    
    if S <= 0 or K <= 0 or sigma <= 0:
        price = max(0, S - K) if option_type == 'C' else max(0, K - S)
        return {
            "price": price,
            "delta": 0.0,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0
        }

    if T <= 0:
        price = max(0, S - K) if option_type == 'C' else max(0, K - S)
        return {
            "price": price,
            "delta": 1.0 if (option_type == 'C' and S > K) else (-1.0 if (option_type == 'P' and S < K) else 0.0),
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0
        }
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    # 公用變數
    pdf_d1 = norm.pdf(d1)
    cdf_d1 = norm.cdf(d1)
    cdf_neg_d1 = norm.cdf(-d1)
    cdf_d2 = norm.cdf(d2)
    cdf_neg_d2 = norm.cdf(-d2)
    
    # Gamma 與 Vega 對 Call/Put 是一樣的
    gamma = pdf_d1 / (S * sigma * np.sqrt(T))
    vega = S * pdf_d1 * np.sqrt(T) / 100 # 通常定義為 IV 變動 1% 的價格變動
    
    if option_type == 'C':
        price = S * cdf_d1 - K * np.exp(-r * T) * cdf_d2
        delta = cdf_d1
        theta = -(S * pdf_d1 * sigma) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * cdf_d2
    else:
        price = K * np.exp(-r * T) * cdf_neg_d2 - S * cdf_neg_d1
        delta = cdf_d1 - 1
        theta = -(S * pdf_d1 * sigma) / (2 * np.sqrt(T)) + r * K * np.exp(-r * T) * cdf_neg_d2
        
    if not np.isfinite(price):
        price = max(0, S - K) if option_type == 'C' else max(0, K - S)
        delta = 0.0
        gamma = 0.0
        theta = 0.0
        vega = 0.0

    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta / 365, # 轉為單日損耗
        "vega": vega
    }

def find_implied_volatility(target_price, S, K, T, r, option_type='C'):
    """使用二分法尋找隱含波動率 (IV)"""
    target_price, S, K, T, r = float(target_price), float(S), float(K), float(T), float(r)
    low, high = 0.0001, 5.0 # 擴大搜尋範圍到 500%
    for _ in range(30): # 增加疊代次數提升精度
        mid = (low + high) / 2
        res = black_scholes(S, K, T, r, mid, option_type)
        if res["price"] < target_price:
            low = mid
        else:
            high = mid
    return mid

def calculate_dte(delivery_date, now=None):
    """計算距離到期剩餘天數 (年化)"""
    now = now or datetime.now()
    if isinstance(delivery_date, str):
        # 支援多種日期格式
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                target = datetime.strptime(delivery_date, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Unknown date format: {delivery_date}")
    else:
        target = delivery_date
    
    delta = (target - now).total_seconds() / (365 * 24 * 3600)
    return max(0.00001, delta)
