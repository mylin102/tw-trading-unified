#!/usr/bin/env python3
"""P1優化：技術指標動態參數調整系統

根據市場波動率動態調整技術指標參數：
1. 高波動率：擴大布林通道寬度，減少假信號
2. 低波動率：縮小布林通道寬度，提高敏感度
3. 極端波動率：暫停交易或使用保守參數
"""

import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional

class DynamicIndicatorAdjuster:
    """動態技術指標參數調整器"""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # 預設參數範圍
        self.param_ranges = {
            "bb_std": {"min": 1.5, "max": 3.0, "default": 2.0},
            "bb_length": {"min": 10, "max": 20, "default": 14},
            "kc_scalar": {"min": 1.0, "max": 2.0, "default": 1.5},
            "kc_length": {"min": 10, "max": 20, "default": 14},
            "atr_multiplier": {"min": 1.5, "max": 3.0, "default": 2.0},
        }
        
        # 波動率狀態
        self.volatility_state = "NORMAL"  # LOW, NORMAL, HIGH, EXTREME
        self.last_adjustment_time = None
        
        # 歷史波動率數據
        self.historical_volatility = []
        self.max_history = 100
        
    def calculate_volatility(self, df: pd.DataFrame, lookback: int = 20) -> float:
        """計算波動率指標"""
        if len(df) < lookback:
            return 0.0
        
        # 使用ATR和價格變動率計算綜合波動率
        close_prices = df["Close"].iloc[-lookback:]
        
        # 1. ATR波動率
        if "atr" in df.columns:
            atr_values = df["atr"].iloc[-lookback:]
            atr_vol = atr_values.mean() / close_prices.mean() if close_prices.mean() > 0 else 0.0
        else:
            # 計算簡易ATR
            high = df["High"].iloc[-lookback:]
            low = df["Low"].iloc[-lookback:]
            close = df["Close"].iloc[-lookback:]
            tr = np.maximum(high - low, 
                          np.maximum(abs(high - close.shift(1)), 
                                   abs(low - close.shift(1))))
            atr_vol = tr.mean() / close_prices.mean() if close_prices.mean() > 0 else 0.0
        
        # 2. 價格變動率波動率
        returns = close_prices.pct_change().dropna()
        if len(returns) > 0:
            price_vol = returns.std() * np.sqrt(252 * 48)  # 年化波動率 (5分鐘數據)
        else:
            price_vol = 0.0
        
        # 3. 布林通道寬度波動率
        if "bb_upper" in df.columns and "bb_lower" in df.columns:
            bb_width = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
            bb_vol = bb_width.iloc[-lookback:].mean()
        else:
            bb_vol = 0.0
        
        # 綜合波動率 (加權平均)
        composite_vol = (atr_vol * 0.4 + price_vol * 0.4 + bb_vol * 0.2)
        
        # 更新歷史波動率
        self.historical_volatility.append(composite_vol)
        if len(self.historical_volatility) > self.max_history:
            self.historical_volatility.pop(0)
        
        return composite_vol
    
    def classify_volatility_state(self, current_vol: float) -> str:
        """根據波動率分類市場狀態"""
        if len(self.historical_volatility) < 10:
            return "NORMAL"
        
        # 計算歷史分位數
        hist_vol_array = np.array(self.historical_volatility)
        q25 = np.percentile(hist_vol_array, 25)
        q75 = np.percentile(hist_vol_array, 75)
        
        if current_vol < q25 * 0.8:
            return "LOW"
        elif current_vol > q75 * 1.2:
            return "HIGH"
        elif current_vol > q75 * 1.5:
            return "EXTREME"
        else:
            return "NORMAL"
    
    def adjust_parameters(self, df: pd.DataFrame) -> Dict:
        """根據波動率調整技術指標參數"""
        # 計算當前波動率
        current_vol = self.calculate_volatility(df, lookback=20)
        
        # 分類波動率狀態
        self.volatility_state = self.classify_volatility_state(current_vol)
        
        # 根據狀態調整參數
        adjusted_params = {}
        
        if self.volatility_state == "LOW":
            # 低波動率：提高敏感度
            adjusted_params = {
                "bb_std": self.param_ranges["bb_std"]["min"] * 1.1,  # 稍微縮小
                "bb_length": self.param_ranges["bb_length"]["min"],
                "kc_scalar": self.param_ranges["kc_scalar"]["min"],
                "kc_length": self.param_ranges["kc_length"]["min"],
                "atr_multiplier": self.param_ranges["atr_multiplier"]["min"],
                "sensitivity": "HIGH"
            }
            
        elif self.volatility_state == "NORMAL":
            # 正常波動率：使用預設參數
            adjusted_params = {
                "bb_std": self.param_ranges["bb_std"]["default"],
                "bb_length": self.param_ranges["bb_length"]["default"],
                "kc_scalar": self.param_ranges["kc_scalar"]["default"],
                "kc_length": self.param_ranges["kc_length"]["default"],
                "atr_multiplier": self.param_ranges["atr_multiplier"]["default"],
                "sensitivity": "NORMAL"
            }
            
        elif self.volatility_state == "HIGH":
            # 高波動率：降低敏感度，擴大通道
            adjusted_params = {
                "bb_std": self.param_ranges["bb_std"]["max"] * 0.9,  # 稍微擴大
                "bb_length": self.param_ranges["bb_length"]["max"],
                "kc_scalar": self.param_ranges["kc_scalar"]["max"],
                "kc_length": self.param_ranges["kc_length"]["max"],
                "atr_multiplier": self.param_ranges["atr_multiplier"]["max"],
                "sensitivity": "LOW"
            }
            
        elif self.volatility_state == "EXTREME":
            # 極端波動率：使用最保守參數
            adjusted_params = {
                "bb_std": self.param_ranges["bb_std"]["max"],
                "bb_length": self.param_ranges["bb_length"]["max"],
                "kc_scalar": self.param_ranges["kc_scalar"]["max"],
                "kc_length": self.param_ranges["kc_length"]["max"],
                "atr_multiplier": self.param_ranges["atr_multiplier"]["max"],
                "sensitivity": "VERY_LOW"
            }
        
        # 添加波動率信息
        adjusted_params.update({
            "volatility": current_vol,
            "volatility_state": self.volatility_state,
            "should_trade": self.volatility_state != "EXTREME"
        })
        
        return adjusted_params
    
    def get_trading_recommendation(self, df: pd.DataFrame) -> Dict:
        """獲取交易建議"""
        adjusted_params = self.adjust_parameters(df)
        
        recommendation = {
            "volatility_state": adjusted_params["volatility_state"],
            "current_volatility": adjusted_params["volatility"],
            "should_trade": adjusted_params["should_trade"],
            "position_size_multiplier": 1.0,
            "stop_loss_adjustment": 1.0,
            "take_profit_adjustment": 1.0
        }
        
        # 根據波動率調整倉位大小和停損
        if adjusted_params["volatility_state"] == "LOW":
            recommendation["position_size_multiplier"] = 1.2  # 低波動率可稍微加大倉位
            recommendation["stop_loss_adjustment"] = 0.9      # 停損可稍微緊一點
            recommendation["take_profit_adjustment"] = 0.9    # 停利可稍微緊一點
            
        elif adjusted_params["volatility_state"] == "HIGH":
            recommendation["position_size_multiplier"] = 0.7  # 高波動率減小倉位
            recommendation["stop_loss_adjustment"] = 1.2      # 停損放寬
            recommendation["take_profit_adjustment"] = 1.2    # 停利放寬
            
        elif adjusted_params["volatility_state"] == "EXTREME":
            recommendation["position_size_multiplier"] = 0.0  # 極端波動率不交易
            recommendation["stop_loss_adjustment"] = 1.5      # 如果必須交易，大幅放寬停損
            recommendation["take_profit_adjustment"] = 1.5
            
        return recommendation


# 單例實例
_dynamic_adjuster = None

def get_dynamic_adjuster(config: Dict = None) -> DynamicIndicatorAdjuster:
    """獲取動態調整器單例"""
    global _dynamic_adjuster
    if _dynamic_adjuster is None:
        _dynamic_adjuster = DynamicIndicatorAdjuster(config)
    return _dynamic_adjuster

def calculate_dynamic_indicators(df: pd.DataFrame, config: Dict = None) -> pd.DataFrame:
    """使用動態參數計算技術指標"""
    from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
    
    adjuster = get_dynamic_adjuster(config)
    adjusted_params = adjuster.adjust_parameters(df)
    
    # 使用調整後的參數計算指標
    dynamic_df = calculate_stock_squeeze(
        df,
        bb_length=int(adjusted_params.get("bb_length", 14)),
        bb_std=adjusted_params.get("bb_std", 2.0),
        kc_length=int(adjusted_params.get("kc_length", 14)),
        kc_scalar=adjusted_params.get("kc_scalar", 1.5)
    )
    
    # 添加動態參數信息
    dynamic_df["dynamic_params"] = str(adjusted_params)
    dynamic_df["volatility_state"] = adjusted_params.get("volatility_state", "NORMAL")
    dynamic_df["current_volatility"] = adjusted_params.get("volatility", 0.0)
    
    return dynamic_df

def get_trading_recommendation(df: pd.DataFrame) -> Dict:
    """獲取基於波動率的交易建議"""
    adjuster = get_dynamic_adjuster()
    return adjuster.get_trading_recommendation(df)