#!/usr/bin/env python3
"""
修復選擇權指標檔 — 補齊缺失的 vwap 和 sqz_on 欄位
從對應日期的期貨指標檔 (TMF) 取得這些欄位
"""
import pandas as pd
import glob
import os
from pathlib import Path

OPTIONS_DIR = Path("strategies/options/logs/paper_trading")
FUTURES_DIR = Path("logs/market_data")

def patch_options_indicators():
    files = sorted(glob.glob(str(OPTIONS_DIR / "OPTIONS_*_indicators.csv")))
    print(f"找到 {len(files)} 個選擇權指標檔")
    
    for f in files:
        df = pd.read_csv(f)
        fname = os.path.basename(f)
        # 從檔名提取日期 (OPTIONS_YYYYMMDD_indicators.csv)
        date_str = fname.split("_")[1]
        
        has_vwap = 'vwap' in df.columns
        has_sqz = 'sqz_on' in df.columns
        
        if has_vwap and has_sqz:
            print(f"  ✅ {fname}: 欄位完整, 跳過")
            continue
        
        # 找對應日期的期貨指標檔
        futures_file = FUTURES_DIR / f"TMF_{date_str}_PAPER_indicators.csv"
        if not futures_file.exists():
            # 試其他 tag
            for tag in ["_LIVE", "", "_DRY"]:
                alt = FUTURES_DIR / f"TMF_{date_str}{tag}_indicators.csv"
                if alt.exists():
                    futures_file = alt
                    break
        
        if not futures_file.exists():
            print(f"  ⚠️ {fname}: 找不到對應期貨檔 ({date_str}), 用預設值")
            df['vwap'] = df.get('price_mtx', 33000)
            df['sqz_on'] = False
        else:
            df_fut = pd.read_csv(futures_file, parse_dates=['timestamp'])
            df_opt = pd.read_csv(f, parse_dates=['timestamp'])
            
            # 重新排序並合併
            df_fut['timestamp'] = pd.to_datetime(df_fut['timestamp'], errors='coerce', format='mixed')
            df_opt['timestamp'] = pd.to_datetime(df_opt['timestamp'], errors='coerce', format='mixed')
            df_fut = df_fut.sort_values('timestamp')
            df_opt = df_opt.sort_values('timestamp')
            
            # 用 timestamp 合併
            if 'timestamp' in df_fut.columns and 'timestamp' in df_opt.columns:
                merged = df_opt.merge(
                    df_fut[['timestamp', 'vwap', 'sqz_on']],
                    on='timestamp',
                    how='left'
                )
                # 如果合併失敗 (時間不對齊), 用簡單方式
                if merged['vwap'].isna().all():
                    print(f"  ⚠️ {fname}: 時間無法對齊, 用 price_mtx 代替 vwap")
                    merged['vwap'] = df_opt.get('price_mtx', 33000)
                    merged['sqz_on'] = False
                else:
                    print(f"  ✅ {fname}: 從期貨檔合併成功")
            else:
                print(f"  ⚠️ {fname}: 無 timestamp 欄位, 用預設值")
                merged = df_opt.copy()
                merged['vwap'] = df_opt.get('price_mtx', 33000)
                merged['sqz_on'] = False
            
            df = merged
        
        # 確保 sqz_on 是 bool
        if 'sqz_on' in df.columns:
            df['sqz_on'] = df['sqz_on'].astype(bool)
        
        # 存回原處
        df.to_csv(f, index=False)
        print(f"  💾 {fname}: {len(df)} bars, vwap={df['vwap'].iloc[-1]:.0f}, sqz_on={df['sqz_on'].sum()}/{len(df)}")

if __name__ == "__main__":
    patch_options_indicators()
