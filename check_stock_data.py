#!/usr/bin/env python3
"""
檢查stock資料完整性並修復問題
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import sys
import yaml

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

DATA_DIR = project_root / "data" / "taifex_raw"

def check_stock_data_integrity():
    """檢查所有觀察清單股票的資料完整性"""
    print("=== STOCK資料完整性檢查 ===")
    
    # 讀取觀察清單
    config_path = project_root / "config" / "stocks.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    watchlist = config.get("stocks", {}).get("watchlist", [])
    
    print(f"觀察清單: {len(watchlist)} 檔股票")
    print()
    
    issues = []
    data_stats = []
    
    for ticker in watchlist:
        # 檢查5分鐘資料
        file_5m = DATA_DIR / f"STOCK_{ticker}_5m.csv"
        file_1d = DATA_DIR / f"STOCK_{ticker}_1d.csv"
        
        stats = {
            "ticker": ticker,
            "5m_exists": file_5m.exists(),
            "1d_exists": file_1d.exists(),
            "5m_rows": 0,
            "1d_rows": 0,
            "5m_date_range": "",
            "1d_date_range": "",
            "issues": []
        }
        
        # 檢查5分鐘資料
        if file_5m.exists():
            try:
                df_5m = pd.read_csv(file_5m)
                stats["5m_rows"] = len(df_5m)
                
                # 檢查時間欄位
                date_col = "Date" if "Date" in df_5m.columns else "timestamp"
                if date_col in df_5m.columns:
                    df_5m[date_col] = pd.to_datetime(df_5m[date_col], errors='coerce')
                    valid_dates = df_5m[date_col].dropna()
                    if len(valid_dates) > 0:
                        stats["5m_date_range"] = f"{valid_dates.min().date()} 到 {valid_dates.max().date()}"
                        
                        # 檢查資料是否足夠
                        if len(df_5m) < 100:
                            stats["issues"].append(f"5分鐘資料不足 ({len(df_5m)}筆)")
                    else:
                        stats["issues"].append("5分鐘資料時間格式錯誤")
                else:
                    stats["issues"].append("5分鐘資料缺少時間欄位")
                    
            except Exception as e:
                stats["issues"].append(f"5分鐘資料讀取錯誤: {e}")
        else:
            stats["issues"].append("缺少5分鐘資料檔案")
        
        # 檢查日線資料
        if file_1d.exists():
            try:
                df_1d = pd.read_csv(file_1d)
                stats["1d_rows"] = len(df_1d)
                
                # 檢查時間欄位
                date_col = "Date" if "Date" in df_1d.columns else "timestamp"
                if date_col in df_1d.columns:
                    df_1d[date_col] = pd.to_datetime(df_1d[date_col], errors='coerce')
                    valid_dates = df_1d[date_col].dropna()
                    if len(valid_dates) > 0:
                        stats["1d_date_range"] = f"{valid_dates.min().date()} 到 {valid_dates.max().date()}"
                        
                        # CANSLIM需要至少200天資料
                        if len(df_1d) < 200:
                            stats["issues"].append(f"日線資料不足CANSLIM需求 ({len(df_1d)}筆)")
                    else:
                        stats["issues"].append("日線資料時間格式錯誤")
                else:
                    stats["issues"].append("日線資料缺少時間欄位")
                    
            except Exception as e:
                stats["issues"].append(f"日線資料讀取錯誤: {e}")
        else:
            stats["issues"].append("缺少日線資料檔案")
        
        data_stats.append(stats)
        
        if stats["issues"]:
            issues.append((ticker, stats["issues"]))
    
    # 顯示統計結果
    print("📊 資料統計:")
    print("-" * 80)
    print(f"{'股票':<6} {'5分鐘資料':<12} {'日線資料':<12} {'問題數':<8} {'備註'}")
    print("-" * 80)
    
    for stats in data_stats:
        ticker = stats["ticker"]
        has_5m = "✅" if stats["5m_exists"] and stats["5m_rows"] > 0 else "❌"
        has_1d = "✅" if stats["1d_exists"] and stats["1d_rows"] > 0 else "❌"
        issue_count = len(stats["issues"])
        
        # 簡要備註
        remark = ""
        if stats["5m_rows"] > 0:
            remark += f"5m:{stats['5m_rows']} "
        if stats["1d_rows"] > 0:
            remark += f"1d:{stats['1d_rows']}"
        
        print(f"{ticker:<6} {has_5m:<12} {has_1d:<12} {issue_count:<8} {remark}")
    
    print()
    
    # 顯示詳細問題
    if issues:
        print("⚠️  發現問題:")
        for ticker, ticker_issues in issues:
            print(f"  {ticker}:")
            for issue in ticker_issues:
                print(f"    - {issue}")
        print()
    
    # 總結
    total_stocks = len(watchlist)
    stocks_with_5m = sum(1 for s in data_stats if s["5m_exists"] and s["5m_rows"] > 0)
    stocks_with_1d = sum(1 for s in data_stats if s["1d_exists"] and s["1d_rows"] > 0)
    stocks_with_issues = len(issues)
    
    print("📈 總結:")
    print(f"  總股票數: {total_stocks}")
    print(f"  有5分鐘資料: {stocks_with_5m}/{total_stocks} ({stocks_with_5m/total_stocks*100:.1f}%)")
    print(f"  有日線資料: {stocks_with_1d}/{total_stocks} ({stocks_with_1d/total_stocks*100:.1f}%)")
    print(f"  有問題的股票: {stocks_with_issues}/{total_stocks}")
    
    return data_stats, issues

def download_missing_data():
    """下載缺失的資料"""
    print("\n=== 下載缺失資料 ===")
    
    try:
        from strategies.stocks.downloader import run_update_all
        
        print("開始下載所有觀察清單股票的資料...")
        run_update_all()
        print("✅ 資料下載完成")
        
    except ImportError as e:
        print(f"❌ 無法匯入下載模組: {e}")
    except Exception as e:
        print(f"❌ 下載過程錯誤: {e}")

def generate_fix_plan(data_stats, issues):
    """生成修復計畫"""
    print("\n=== 資料修復計畫 ===")
    
    # 分類問題
    critical_issues = []  # 完全沒有資料
    warning_issues = []   # 資料不足
    minor_issues = []     # 其他問題
    
    for ticker, ticker_issues in issues:
        for issue in ticker_issues:
            if "缺少" in issue or "讀取錯誤" in issue:
                critical_issues.append((ticker, issue))
            elif "不足" in issue:
                warning_issues.append((ticker, issue))
            else:
                minor_issues.append((ticker, issue))
    
    print("1. 嚴重問題 (需要立即處理):")
    if critical_issues:
        for ticker, issue in critical_issues[:5]:  # 只顯示前5個
            print(f"   - {ticker}: {issue}")
        if len(critical_issues) > 5:
            print(f"   ... 還有 {len(critical_issues) - 5} 個嚴重問題")
    else:
        print("   ✅ 無嚴重問題")
    
    print("\n2. 警告問題 (建議處理):")
    if warning_issues:
        for ticker, issue in warning_issues[:5]:
            print(f"   - {ticker}: {issue}")
        if len(warning_issues) > 5:
            print(f"   ... 還有 {len(warning_issues) - 5} 個警告問題")
    else:
        print("   ✅ 無警告問題")
    
    print("\n3. 建議行動:")
    if critical_issues or warning_issues:
        print("   a) 執行資料下載: python3 strategies/stocks/downloader.py")
        print("   b) 檢查網路連線和API權限")
        print("   c) 手動下載缺失資料並匯入")
    else:
        print("   ✅ 資料狀態良好，無需立即行動")
    
    print("\n4. 長期改善:")
    print("   a) 設定定期資料更新排程")
    print("   b) 加入資料品質監控告警")
    print("   c) 建立資料備份和恢復機制")

def main():
    """主程式"""
    print("=" * 60)
    print("STOCK資料完整性檢查與修復工具")
    print("=" * 60)
    
    # 檢查資料完整性
    data_stats, issues = check_stock_data_integrity()
    
    # 如果有問題，提供修復選項
    if issues:
        print("\n" + "=" * 60)
        response = input("是否要下載缺失資料？ (y/n): ")
        if response.lower() == 'y':
            download_missing_data()
    
    # 生成修復計畫
    generate_fix_plan(data_stats, issues)
    
    # 最終建議
    print("\n" + "=" * 60)
    print("最終建議:")
    
    issue_count = sum(len(i[1]) for i in issues)
    if issue_count == 0:
        print("✅ 所有股票資料完整，系統準備就緒")
    elif issue_count <= 5:
        print("⚠️  有少量資料問題，但不影響基本運作")
        print("   建議在交易前修復這些問題")
    else:
        print("❌ 有較多資料問題，可能影響策略效果")
        print("   強烈建議在交易前修復這些問題")
    
    print("\n明日交易準備:")
    print("1. 執行完整測試: python3 scripts/tools/test_stock_integration.py")
    print("2. 乾跑測試: python3 scripts/runners/dry_run_stocks.py")
    print("3. 確認配置: config/stocks.yaml 和 .env")
    print("4. 監控系統日誌: logs/ 目錄")

if __name__ == "__main__":
    main()