#!/usr/bin/env python3
"""P2優化：多時間框架確認系統

功能：
1. 15分鐘趨勢過濾 - 避免逆勢交易
2. 60分鐘市場狀態識別 - 趨勢/盤整判斷
3. 多時間框架一致性檢查
"""

import pandas as pd
import numpy as np
from typing import Dict, Tuple, Optional
from datetime import datetime, timedelta

class MultiTimeframeAnalyzer:
    """多時間框架分析器"""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # 時間框架配置
        self.timeframes = {
            '5m': {'name': '5分鐘', 'bars_for_trend': 20},  # 交易時間框架
            '15m': {'name': '15分鐘', 'bars_for_trend': 10},  # 短期趨勢
            '60m': {'name': '60分鐘', 'bars_for_trend': 5}   # 中期趨勢
        }
        
        # 趨勢判斷參數
        self.trend_params = {
            'strong_trend_threshold': 0.02,  # 2%以上為強趨勢
            'weak_trend_threshold': 0.005,   # 0.5%以上為弱趨勢
            'consolidation_threshold': 0.002 # 0.2%以下為盤整
        }
        
        # 市場狀態
        self.market_state = {
            'primary_trend': 'UNKNOWN',  # 主要趨勢
            'market_regime': 'NEUTRAL',  # 市場狀態
            'volatility_regime': 'NORMAL' # 波動率狀態
        }
        
    def resample_data(self, df_5m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """將5分鐘數據重採樣到其他時間框架"""
        if timeframe == '5m':
            return df_5m.copy()
        
        # 定義重採樣規則
        resample_rules = {
            '15m': '15min',
            '60m': '60min'
        }
        
        if timeframe not in resample_rules:
            raise ValueError(f"不支援的時間框架: {timeframe}")
        
        # 重採樣
        resampled = df_5m.resample(resample_rules[timeframe]).agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }).dropna()
        
        return resampled
    
    def analyze_trend(self, df: pd.DataFrame, timeframe: str) -> Dict:
        """分析特定時間框架的趨勢"""
        if len(df) < 20:
            return {'trend': 'UNKNOWN', 'strength': 0, 'direction': 0}
        
        # 計算移動平均線
        df = df.copy()
        df['ma_fast'] = df['Close'].rolling(10).mean()
        df['ma_slow'] = df['Close'].rolling(30).mean()
        
        # 計算價格變動
        recent_prices = df['Close'].iloc[-10:]
        price_change = (recent_prices.iloc[-1] - recent_prices.iloc[0]) / recent_prices.iloc[0]
        
        # 判斷趨勢方向
        if price_change > self.trend_params['strong_trend_threshold']:
            trend = 'STRONG_UP'
            strength = abs(price_change)
        elif price_change > self.trend_params['weak_trend_threshold']:
            trend = 'WEAK_UP'
            strength = abs(price_change)
        elif price_change < -self.trend_params['strong_trend_threshold']:
            trend = 'STRONG_DOWN'
            strength = abs(price_change)
        elif price_change < -self.trend_params['weak_trend_threshold']:
            trend = 'WEAK_DOWN'
            strength = abs(price_change)
        elif abs(price_change) < self.trend_params['consolidation_threshold']:
            trend = 'CONSOLIDATION'
            strength = 0
        else:
            trend = 'NEUTRAL'
            strength = abs(price_change)
        
        # 計算技術指標
        rsi = self._calculate_rsi(df['Close'])
        atr = self._calculate_atr(df)
        
        return {
            'timeframe': timeframe,
            'trend': trend,
            'strength': strength,
            'direction': 1 if price_change > 0 else -1,
            'price_change_pct': price_change * 100,
            'ma_fast': df['ma_fast'].iloc[-1],
            'ma_slow': df['ma_slow'].iloc[-1],
            'ma_alignment': 'BULL' if df['ma_fast'].iloc[-1] > df['ma_slow'].iloc[-1] else 'BEAR',
            'rsi': rsi.iloc[-1] if len(rsi) > 0 else 50,
            'atr': atr.iloc[-1] if len(atr) > 0 else 0,
            'atr_pct': (atr.iloc[-1] / df['Close'].iloc[-1]) * 100 if len(atr) > 0 and df['Close'].iloc[-1] > 0 else 0
        }
    
    def analyze_multi_timeframe(self, df_5m: pd.DataFrame) -> Dict:
        """多時間框架綜合分析"""
        results = {}
        
        # 分析各時間框架
        for tf_key, tf_info in self.timeframes.items():
            if tf_key == '5m':
                df_tf = df_5m
            else:
                df_tf = self.resample_data(df_5m, tf_key)
            
            if len(df_tf) >= 20:
                trend_analysis = self.analyze_trend(df_tf, tf_key)
                results[tf_key] = trend_analysis
            else:
                results[tf_key] = {'trend': 'INSUFFICIENT_DATA', 'timeframe': tf_key}
        
        # 綜合判斷
        if '5m' in results and '15m' in results and '60m' in results:
            self._update_market_state(results)
        
        return {
            'timeframe_analysis': results,
            'market_state': self.market_state,
            'trading_recommendation': self._generate_trading_recommendation(results)
        }
    
    def _update_market_state(self, results: Dict):
        """更新市場狀態"""
        # 判斷主要趨勢（以60分鐘為準）
        tf_60m = results.get('60m', {})
        if tf_60m.get('trend', 'UNKNOWN') in ['STRONG_UP', 'WEAK_UP']:
            self.market_state['primary_trend'] = 'BULL'
        elif tf_60m.get('trend', 'UNKNOWN') in ['STRONG_DOWN', 'WEAK_DOWN']:
            self.market_state['primary_trend'] = 'BEAR'
        else:
            self.market_state['primary_trend'] = 'NEUTRAL'
        
        # 判斷市場狀態
        trends = [results[tf].get('trend', 'UNKNOWN') for tf in ['5m', '15m', '60m']]
        
        # 檢查趨勢一致性
        up_count = sum(1 for t in trends if 'UP' in t)
        down_count = sum(1 for t in trends if 'DOWN' in t)
        consolidation_count = sum(1 for t in trends if t == 'CONSOLIDATION')
        
        if up_count >= 2:
            self.market_state['market_regime'] = 'TRENDING_UP'
        elif down_count >= 2:
            self.market_state['market_regime'] = 'TRENDING_DOWN'
        elif consolidation_count >= 2:
            self.market_state['market_regime'] = 'CONSOLIDATION'
        else:
            self.market_state['market_regime'] = 'MIXED'
        
        # 判斷波動率狀態
        atr_values = [results[tf].get('atr_pct', 0) for tf in ['5m', '15m', '60m'] if 'atr_pct' in results[tf]]
        if atr_values:
            avg_atr = np.mean(atr_values)
            if avg_atr > 1.5:
                self.market_state['volatility_regime'] = 'HIGH'
            elif avg_atr < 0.5:
                self.market_state['volatility_regime'] = 'LOW'
            else:
                self.market_state['volatility_regime'] = 'NORMAL'
    
    def _generate_trading_recommendation(self, results: Dict) -> Dict:
        """生成交易建議"""
        recommendation = {
            'should_trade': True,
            'position_size_multiplier': 1.0,
            'risk_multiplier': 1.0,
            'reason': 'NORMAL_CONDITIONS',
            'filters_passed': 0,
            'total_filters': 4
        }
        
        # 檢查各時間框架趨勢
        filters_passed = 0
        
        # 1. 60分鐘趨勢過濾（避免逆勢交易）
        tf_60m = results.get('60m', {})
        if tf_60m.get('trend', 'UNKNOWN') not in ['STRONG_DOWN', 'WEAK_DOWN']:
            filters_passed += 1
        else:
            recommendation['should_trade'] = False
            recommendation['reason'] = '60M_DOWNTREND'
        
        # 2. 15分鐘趨勢確認
        tf_15m = results.get('15m', {})
        if tf_15m.get('trend', 'UNKNOWN') != 'STRONG_DOWN':
            filters_passed += 1
        elif recommendation['should_trade']:
            recommendation['position_size_multiplier'] *= 0.5
            recommendation['risk_multiplier'] *= 1.5
        
        # 3. 波動率過濾
        if self.market_state['volatility_regime'] != 'HIGH':
            filters_passed += 1
        elif recommendation['should_trade']:
            recommendation['position_size_multiplier'] *= 0.7
            recommendation['risk_multiplier'] *= 1.3
        
        # 4. 市場狀態過濾
        if self.market_state['market_regime'] not in ['TRENDING_DOWN', 'MIXED']:
            filters_passed += 1
        elif recommendation['should_trade']:
            recommendation['position_size_multiplier'] *= 0.8
        
        recommendation['filters_passed'] = filters_passed
        
        # 根據過濾結果調整
        if filters_passed >= 3:
            recommendation['position_size_multiplier'] = min(1.2, recommendation['position_size_multiplier'] * 1.1)
        elif filters_passed <= 1:
            recommendation['should_trade'] = False
            recommendation['reason'] = f'TOO_MANY_FILTERS_FAILED ({filters_passed}/4)'
        
        return recommendation
    
    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """計算RSI"""
        if len(prices) < period + 1:
            return pd.Series([50] * len(prices), index=prices.index)
        
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """計算ATR"""
        if len(df) < period + 1:
            return pd.Series([0] * len(df), index=df.index)
        
        high = df['High']
        low = df['Low']
        close = df['Close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        
        return atr
    
    def get_trend_alignment_score(self, results: Dict) -> float:
        """計算趨勢一致性分數（0-1）"""
        if not all(tf in results for tf in ['5m', '15m', '60m']):
            return 0.0
        
        trends = []
        for tf in ['5m', '15m', '60m']:
            trend = results[tf].get('trend', 'UNKNOWN')
            if 'UP' in trend:
                trends.append(1)
            elif 'DOWN' in trend:
                trends.append(-1)
            else:
                trends.append(0)
        
        # 計算一致性（同號的數量）
        alignment = sum(1 for i in range(len(trends)-1) if trends[i] * trends[i+1] > 0)
        return alignment / (len(trends) - 1) if len(trends) > 1 else 0.0


# 單例實例
_multi_tf_analyzer = None

def get_multi_tf_analyzer(config: Dict = None) -> MultiTimeframeAnalyzer:
    """獲取多時間框架分析器單例"""
    global _multi_tf_analyzer
    if _multi_tf_analyzer is None:
        _multi_tf_analyzer = MultiTimeframeAnalyzer(config)
    return _multi_tf_analyzer

def analyze_market_condition(df_5m: pd.DataFrame) -> Dict:
    """分析市場條件"""
    analyzer = get_multi_tf_analyzer()
    return analyzer.analyze_multi_timeframe(df_5m)

def should_trade_based_on_tf(df_5m: pd.DataFrame) -> Tuple[bool, Dict]:
    """基於多時間框架判斷是否應該交易"""
    analysis = analyze_market_condition(df_5m)
    recommendation = analysis.get('trading_recommendation', {})
    return recommendation.get('should_trade', False), analysis