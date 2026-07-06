#!/usr/bin/env python3
"""
數據品質檢查腳本
用於監控夜盤交易數據品質
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import os
from pathlib import Path

class DataQualityMonitor:
    """數據品質監控器"""
    
    def __init__(self, log_dir="logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.quality_log = self.log_dir / "data_quality.log"
        
    def check_kbar_intervals(self, df, timeframe="5m"):
        """檢查K線間隔"""
        if df is None or len(df) < 2:
            return {"status": "error", "message": "數據不足"}
        
        # 確保有timestamp列
        if 'timestamp' not in df.columns:
            return {"status": "error", "message": "缺少timestamp列"}
        
        # 轉換時間戳
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp')
        
        # 計算時間間隔
        intervals = df['timestamp'].diff().dropna()
        
        if len(intervals) == 0:
            return {"status": "error", "message": "無法計算間隔"}
        
        # 預期間隔
        if timeframe == "5m":
            expected_interval = timedelta(minutes=5)
        elif timeframe == "15m":
            expected_interval = timedelta(minutes=15)
        elif timeframe == "1h":
            expected_interval = timedelta(hours=1)
        else:
            expected_interval = timedelta(minutes=5)
        
        # 檢查異常
        min_interval = intervals.min()
        max_interval = intervals.max()
        median_interval = intervals.median()
        
        # 判斷標準
        issues = []
        
        if max_interval > expected_interval * 2:
            issues.append(f"最大間隔異常: {max_interval} (預期: {expected_interval})")
        
        if min_interval < expected_interval / 2:
            issues.append(f"最小間隔異常: {min_interval} (預期: {expected_interval})")
        
        # 計算缺失K線數量
        expected_count = (df['timestamp'].max() - df['timestamp'].min()) / expected_interval
        actual_count = len(df)
        missing_rate = 1 - (actual_count / expected_count)
        
        if missing_rate > 0.1:  # 缺失超過10%
            issues.append(f"K線缺失率過高: {missing_rate:.1%}")
        
        status = "warning" if issues else "ok"
        
        return {
            "status": status,
            "timeframe": timeframe,
            "min_interval": str(min_interval),
            "max_interval": str(max_interval),
            "median_interval": str(median_interval),
            "expected_interval": str(expected_interval),
            "data_points": len(df),
            "missing_rate": missing_rate,
            "issues": issues
        }
    
    def check_data_freshness(self, df, max_age_minutes=2):
        """檢查數據新鮮度"""
        if df is None or len(df) == 0:
            return {"status": "error", "message": "無數據"}
        
        if 'timestamp' not in df.columns:
            return {"status": "error", "message": "缺少timestamp列"}
        
        # 獲取最新數據時間
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        latest_time = df['timestamp'].max()
        current_time = datetime.now()
        
        # 計算年齡
        age = current_time - latest_time
        age_minutes = age.total_seconds() / 60
        
        status = "warning" if age_minutes > max_age_minutes else "ok"
        
        return {
            "status": status,
            "latest_data_time": latest_time.strftime("%Y-%m-%d %H:%M:%S"),
            "current_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "age_minutes": age_minutes,
            "max_allowed_age": max_age_minutes,
            "issues": [f"數據延遲: {age_minutes:.1f}分鐘"] if age_minutes > max_age_minutes else []
        }
    
    def check_price_validity(self, df):
        """檢查價格有效性"""
        if df is None or len(df) == 0:
            return {"status": "error", "message": "無數據"}
        
        required_columns = ['Open', 'High', 'Low', 'Close']
        missing_cols = [col for col in required_columns if col not in df.columns]
        
        if missing_cols:
            return {"status": "error", "message": f"缺少價格列: {missing_cols}"}
        
        issues = []
        
        # 檢查價格合理性
        for idx, row in df.iterrows():
            # 檢查High >= Low
            if row['High'] < row['Low']:
                issues.append(f"行{idx}: High({row['High']}) < Low({row['Low']})")
            
            # 檢查Close在High/Low範圍內
            if row['Close'] > row['High'] or row['Close'] < row['Low']:
                issues.append(f"行{idx}: Close({row['Close']})超出範圍[{row['Low']}, {row['High']}]")
            
            # 檢查Open在High/Low範圍內
            if row['Open'] > row['High'] or row['Open'] < row['Low']:
                issues.append(f"行{idx}: Open({row['Open']})超出範圍[{row['Low']}, {row['High']}]")
        
        status = "warning" if issues else "ok"
        
        return {
            "status": status,
            "rows_checked": len(df),
            "price_issues": len(issues),
            "issues": issues[:10]  # 只顯示前10個問題
        }
    
    def check_volume_validity(self, df):
        """檢查成交量有效性"""
        if df is None or len(df) == 0:
            return {"status": "error", "message": "無數據"}
        
        if 'Volume' not in df.columns:
            return {"status": "warning", "message": "缺少Volume列"}
        
        issues = []
        
        # 檢查成交量非負
        negative_volume = df[df['Volume'] < 0]
        if len(negative_volume) > 0:
            issues.append(f"負成交量: {len(negative_volume)}筆")
        
        # 檢查異常大成交量
        if len(df) > 1:
            volume_mean = df['Volume'].mean()
            volume_std = df['Volume'].std()
            threshold = volume_mean + 5 * volume_std
            extreme_volume = df[df['Volume'] > threshold]
            if len(extreme_volume) > 0:
                issues.append(f"異常大成交量: {len(extreme_volume)}筆")
        
        status = "warning" if issues else "ok"
        
        return {
            "status": status,
            "volume_issues": len(issues),
            "issues": issues
        }
    
    def log_quality_report(self, report):
        """記錄品質報告"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        with open(self.quality_log, 'a') as f:
            f.write(f"\n=== 數據品質報告 {timestamp} ===\n")
            
            for check_name, result in report.items():
                status_icon = "✅" if result['status'] == 'ok' else "⚠️" if result['status'] == 'warning' else "❌"
                f.write(f"\n{status_icon} {check_name}:\n")
                
                for key, value in result.items():
                    if key not in ['status', 'issues']:
                        f.write(f"  {key}: {value}\n")
                
                if result.get('issues'):
                    f.write(f"  問題:\n")
                    for issue in result['issues']:
                        f.write(f"    - {issue}\n")
            
            f.write("\n" + "="*50 + "\n")
    
    def run_comprehensive_check(self, data_dict):
        """運行全面檢查"""
        report = {}
        
        # 檢查每個時間框架
        for timeframe, df in data_dict.items():
            if df is not None and len(df) > 0:
                report[f"K線間隔檢查_{timeframe}"] = self.check_kbar_intervals(df, timeframe)
                report[f"數據新鮮度檢查_{timeframe}"] = self.check_data_freshness(df)
                report[f"價格有效性檢查_{timeframe}"] = self.check_price_validity(df)
                report[f"成交量檢查_{timeframe}"] = self.check_volume_validity(df)
        
        # 記錄報告
        self.log_quality_report(report)
        
        # 總結
        total_checks = len(report)
        ok_checks = sum(1 for r in report.values() if r['status'] == 'ok')
        warning_checks = sum(1 for r in report.values() if r['status'] == 'warning')
        error_checks = sum(1 for r in report.values() if r['status'] == 'error')
        
        summary = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_checks": total_checks,
            "ok_checks": ok_checks,
            "warning_checks": warning_checks,
            "error_checks": error_checks,
            "quality_score": ok_checks / total_checks if total_checks > 0 else 0
        }
        
        return summary, report

def load_sample_data():
    """載入樣本數據用於測試"""
    # 這裡可以替換為實際數據載入邏輯
    data_dir = Path("data/taifex_raw")
    
    data_dict = {}
    
    # 嘗試載入期貨數據
    futures_file = Path("data/tmf_full_2026.csv")
    if futures_file.exists():
        try:
            df_futures = pd.read_csv(futures_file, nrows=1000)  # 只讀取部分數據
            data_dict["futures_5m"] = df_futures
        except Exception as e:
            print(f"載入期貨數據失敗: {e}")
    
    return data_dict

def main():
    """主函數"""
    print("="*60)
    print("          數據品質檢查系統")
    print("="*60)
    
    # 初始化監控器
    monitor = DataQualityMonitor()
    
    # 載入數據
    print("\n📊 載入數據...")
    data_dict = load_sample_data()
    
    if not data_dict:
        print("❌ 無數據可檢查")
        return
    
    print(f"✅ 載入 {len(data_dict)} 個數據集")
    
    # 運行檢查
    print("\n🔍 運行數據品質檢查...")
    summary, report = monitor.run_comprehensive_check(data_dict)
    
    # 顯示結果
    print("\n" + "="*60)
    print("          檢查結果")
    print("="*60)
    
    print(f"\n📈 品質分數: {summary['quality_score']:.1%}")
    print(f"✅ 通過檢查: {summary['ok_checks']}/{summary['total_checks']}")
    print(f"⚠️  警告檢查: {summary['warning_checks']}/{summary['total_checks']}")
    print(f"❌ 錯誤檢查: {summary['error_checks']}/{summary['total_checks']}")
    
    # 顯示詳細問題
    print("\n🔎 詳細問題:")
    for check_name, result in report.items():
        if result['status'] != 'ok':
            status_icon = "⚠️" if result['status'] == 'warning' else "❌"
            print(f"\n{status_icon} {check_name}:")
            if result.get('issues'):
                for issue in result['issues'][:3]:  # 只顯示前3個問題
                    print(f"  - {issue}")
                if len(result['issues']) > 3:
                    print(f"  ... 還有 {len(result['issues']) - 3} 個問題")
    
    # 建議
    print("\n💡 建議:")
    if summary['warning_checks'] > 0 or summary['error_checks'] > 0:
        print("  1. 檢查數據源連接")
        print("  2. 驗證數據格式一致性")
        print("  3. 監控數據更新頻率")
    else:
        print("  數據品質良好，繼續保持!")
    
    print(f"\n📝 詳細報告已保存至: {monitor.quality_log}")
    
    return summary

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n操作被用戶中斷")
    except Exception as e:
        print(f"\n❌ 程序錯誤: {str(e)}")
        import traceback
        traceback.print_exc()