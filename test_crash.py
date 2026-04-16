#!/usr/bin/env python3
"""
測試 PM2 自動重啟的腳本
每運行30秒後故意崩潰一次
"""
import time
import sys
import os
from datetime import datetime

def main():
    start_time = time.time()
    pid = os.getpid()
    
    print(f"[{datetime.now()}] 🔧 測試腳本啟動 (PID: {pid})")
    
    # 運行一段時間後故意崩潰
    while True:
        elapsed = time.time() - start_time
        print(f"[{datetime.now()}] ⏱️  已運行 {elapsed:.1f}秒")
        
        # 每30秒崩潰一次
        if elapsed > 30:
            print(f"[{datetime.now()}] 💥 故意崩潰！")
            # 故意引發錯誤
            raise RuntimeError("測試崩潰")
        
        time.sleep(5)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[{datetime.now()}] ❌ 崩潰原因: {e}")
        sys.exit(1)