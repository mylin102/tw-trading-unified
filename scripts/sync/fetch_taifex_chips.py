"""
Taifex Chip Fetcher — Wave 17.
Downloads historical institutional open interest data from Taifex.
Focus: Foreign Investors (Foreign), Investment Trusts (Trust), Dealers (Dealers).
"""
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from tqdm import tqdm
import time

def fetch_chips(years=3):
    print(f"🚀 Fetching Institutional Chips for {years} years...")
    end_date = datetime.now()
    start_date = end_date - timedelta(days=years * 365)
    
    current = start_date
    all_data = []
    
    # URL for Taifex Three Big Institutional Investors (Futures)
    # query_date: yyyy/mm/dd
    URL = "https://www.taifex.com.tw/cht/3/futInstitutionalSettlementQuery"
    
    while current <= end_date:
        date_str = current.strftime("%Y/%m/%%d") # Wait, correct format is yyyy/mm/dd
        date_query = current.strftime("%Y/%m/%d")
        
        # Note: Scrapers need careful rate limiting
        # In this environment, we'll simulate or use a mock if network is restricted
        # However, for R&D we try to get real data.
        
        # For Wave 17, I'll implement the logic to parse the CSV version which is faster
        CSV_URL = f"https://www.taifex.com.tw/cht/3/futInstitutionalSettlementDown?queryDate={date_query}&comma=Y"
        
        try:
            # We filter for 'TX' (台股期貨)
            res = requests.get(CSV_URL, timeout=10)
            if res.status_code == 200 and "日期" in res.text:
                # Convert to DF
                from io import StringIO
                df = pd.read_csv(StringIO(res.text))
                # Filter for TX (臺股期貨)
                tx_df = df[df['商品名稱'].str.contains('臺股期貨', na=False)]
                
                if not tx_df.empty:
                    # Extract Key Metrics: Foreign Net Position
                    # Column 1: Identity, Column 13: Net Position
                    foreign = tx_df[tx_df['身份別'] == '外資']['多空淨額'].iloc[0]
                    trust = tx_df[tx_df['身份別'] == '投信']['多空淨額'].iloc[0]
                    dealers = tx_df[tx_df['身份別'] == '自營商']['多空淨額'].iloc[0]
                    
                    all_data.append({
                        "date": current.date(),
                        "foreign_net": int(foreign),
                        "trust_net": int(trust),
                        "dealers_net": int(dealers)
                    })
                    print(f" ✅ {date_query}: Foreign Net = {foreign}")
        except:
            pass
            
        current += timedelta(days=1)
        time.sleep(0.5) # Be kind to Taifex

    if all_data:
        chip_df = pd.DataFrame(all_data)
        out_path = Path("data/chips/taifex_institutional.parquet")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        chip_df.to_parquet(out_path)
        print(f"✨ Saved {len(chip_df)} days of chip data to {out_path}")

if __name__ == "__main__":
    fetch_chips(years=1) # Start with 1 year for safety
