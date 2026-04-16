#!/usr/bin/env python3
"""
修復版交易系統 - 使用正確的 Shioaji 1.3.3 API
"""
import os
import sys
import time
import traceback
import signal
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
import shioaji as sj

console = Console()

# 全局變量
_shutdown_event = threading.Event()
_api = None

def signal_handler(signum, frame):
    """信號處理器"""
    console.print(f"[yellow]📴 收到信號 {signum}，優雅關閉中...[/yellow]")
    _shutdown_event.set()
    time.sleep(1)
    sys.exit(0)

def safe_shioaji_test():
    """安全的 Shioaji 測試"""
    global _api
    
    try:
        console.print("[blue]🔧 初始化 Shioaji...[/blue]")
        _api = sj.Shioaji()
        
        # 登入
        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        
        if not api_key or not secret_key:
            console.print("[red]❌ 缺少 API 金鑰或密鑰[/red]")
            return False
        
        console.print(f"[dim]🔑 使用 API 金鑰: {api_key[:8]}...[/dim]")
        
        # 登入
        console.print("[dim]🔄 登入中...[/dim]")
        _api.login(api_key=api_key, secret_key=secret_key, fetch_contract=True)
        console.print("[green]✅ 登入成功[/green]")
        
        # 獲取合約
        console.print("[dim]📋 獲取合約中...[/dim]")
        
        # 期貨合約
        tmf_contracts = _api.Contracts.Futures["TMF"]
        mxf_contracts = _api.Contracts.Futures["MXF"]
        
        # 選擇最近月合約
        def get_nearest_contract(contracts):
            valid = []
            for contract in contracts:
                if hasattr(contract, 'delivery_date'):
                    try:
                        # 簡單檢查合約是否有效
                        if contract.delivery_date >= "2026-04-14":
                            valid.append(contract)
                    except:
                        continue
            
            if not valid:
                return None
            
            valid.sort(key=lambda x: x.delivery_date)
            return valid[0]
        
        tmf_contract = get_nearest_contract(tmf_contracts)
        mxf_contract = get_nearest_contract(mxf_contracts)
        
        if not tmf_contract or not mxf_contract:
            console.print("[red]❌ 找不到有效合約[/red]")
            return False
        
        console.print(f"[green]📈 TMF 合約: {tmf_contract.code} (到期日: {tmf_contract.delivery_date})[/green]")
        console.print(f"[green]📈 MXF 合約: {mxf_contract.code} (到期日: {mxf_contract.delivery_date})[/green]")
        
        # 定義回調函數
        tick_count = {"tmf": 0, "mxf": 0, "total": 0}
        
        def on_tick(exchange, tick):
            """Tick 回調函數"""
            if _shutdown_event.is_set():
                return
            
            tick_count["total"] += 1
            
            # 簡單記錄
            if tick_count["total"] <= 10:
                console.print(f"[cyan]📥 Tick #{tick_count['total']}: {tick.code} = {tick.close}[/cyan]")
            elif tick_count["total"] % 50 == 0:
                console.print(f"[dim]📊 已接收 {tick_count['total']} 個tick[/dim]")
        
        def on_bidask(exchange, bidask):
            """Bid/Ask 回調函數"""
            if _shutdown_event.is_set():
                return
            
            # 簡單記錄
            if hasattr(bidask, 'code'):
                bid = bidask.bid_price[0] if hasattr(bidask.bid_price, '__getitem__') else bidask.bid_price
                ask = bidask.ask_price[0] if hasattr(bidask.ask_price, '__getitem__') else bidask.ask_price
                console.print(f"[magenta]💰 BidAsk: {bidask.code} bid={bid} ask={ask}[/magenta]")
        
        # 設置回調 (Shioaji 1.3.3 正確用法)
        console.print("[dim]🎯 設置回調函數...[/dim]")
        _api.quote.set_on_tick_fop_v1_callback(on_tick)
        _api.quote.set_on_bidask_fop_v1_callback(on_bidask)
        
        # 訂閱合約
        console.print("[dim]📡 訂閱行情中...[/dim]")
        _api.quote.subscribe(tmf_contract, quote_type=sj.constant.QuoteType.Tick)
        _api.quote.subscribe(tmf_contract, quote_type=sj.constant.QuoteType.BidAsk)
        _api.quote.subscribe(mxf_contract, quote_type=sj.constant.QuoteType.Tick)
        _api.quote.subscribe(mxf_contract, quote_type=sj.constant.QuoteType.BidAsk)
        
        console.print("[green]✅ 訂閱成功[/green]")
        console.print("[green]🚀 系統運行中...[/green]")
        
        # 主循環
        start_time = time.time()
        while not _shutdown_event.is_set():
            elapsed = time.time() - start_time
            console.print(f"[dim]⏱️  運行時間: {elapsed:.0f}秒 | Tick數: {tick_count['total']}[/dim]")
            
            # 每10秒檢查一次
            for _ in range(10):
                if _shutdown_event.is_set():
                    break
                time.sleep(1)
        
        console.print("[yellow]🔄 關閉中...[/yellow]")
        
        # 取消訂閱
        _api.quote.unsubscribe(tmf_contract)
        _api.quote.unsubscribe(mxf_contract)
        
        # 登出
        _api.logout()
        
        console.print("[green]✅ 系統正常關閉[/green]")
        return True
        
    except Exception as e:
        console.print(f"[red]❌ 發生錯誤:[/red]")
        console.print(traceback.format_exc())
        
        # 記錄錯誤
        error_log = Path("logs") / "shioaji_crash.log"
        error_log.parent.mkdir(exist_ok=True)
        
        with open(error_log, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"時間: {datetime.now()}\n")
            f.write(f"錯誤: {e}\n")
            f.write(traceback.format_exc())
            f.write(f"\n{'='*60}\n")
        
        # 嘗試清理
        try:
            if _api:
                _api.logout()
        except:
            pass
        
        return False

def main():
    """主函數"""
    console.print("[bold blue]🔧 修復版交易系統啟動[/bold blue]")
    console.print(f"[dim]時間: {datetime.now()}[/dim]")
    console.print(f"[dim]Shioaji 版本: {sj.__version__}[/dim]")
    
    # 註冊信號處理器
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        # 運行系統
        success = safe_shioaji_test()
        
        if success:
            console.print("[bold green]✅ 系統運行成功[/bold green]")
        else:
            console.print("[bold red]❌ 系統運行失敗[/bold red]")
            
    except KeyboardInterrupt:
        console.print("[yellow]🛑 用戶中斷[/yellow]")
        _shutdown_event.set()
        
    except Exception as e:
        console.print(f"[bold red]💥 未處理的異常: {e}[/bold red]")
        console.print(traceback.format_exc())
        
    finally:
        console.print("[dim]👋 程式結束[/dim]")

if __name__ == "__main__":
    main()