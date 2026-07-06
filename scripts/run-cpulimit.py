#!/usr/bin/env python3
# 2026-06-23 Gemini CLI: Run a subprocess limited to 50% CPU on macOS/Linux (duty-cycle SIGSTOP/SIGCONT)

import os
import sys
import time
import signal
import subprocess
import threading

def get_all_children(parent_pid):
    pids = []
    try:
        output = subprocess.check_output(["pgrep", "-P", str(parent_pid)], text=True)
        children = [int(p.strip()) for p in output.strip().split("\n") if p.strip().isdigit()]
        for child in children:
            pids.append(child)
            pids.extend(get_all_children(child))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return pids

def limit_thread_func(pid, limit_percent, stop_event):
    duty_cycle = limit_percent / 100.0
    interval = 0.05  # 50ms interval
    run_time = interval * duty_cycle
    stop_time = interval * (1.0 - duty_cycle)
    
    try:
        while not stop_event.is_set():
            # Resume parent and all child processes
            pids = [pid] + get_all_children(pid)
            for p in pids:
                try:
                    os.kill(p, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            time.sleep(run_time)
            
            if not stop_event.is_set():
                # Suspend parent and all child processes
                pids = [pid] + get_all_children(pid)
                for p in pids:
                    try:
                        os.kill(p, signal.SIGSTOP)
                    except ProcessLookupError:
                        pass
                time.sleep(stop_time)
    except Exception:
        pass
    finally:
        # Guarantee all processes are left resumed on exit
        pids = [pid] + get_all_children(pid)
        for p in pids:
            try:
                os.kill(p, signal.SIGCONT)
            except ProcessLookupError:
                pass

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 run-cpulimit.py [command] [args...]")
        sys.exit(1)
        
    cmd = sys.argv[1:]
    
    # Start target process
    proc = subprocess.Popen(cmd)
    
    stop_event = threading.Event()
    limiter = threading.Thread(
        target=limit_thread_func,
        args=(proc.pid, 50, stop_event),
        daemon=True
    )
    limiter.start()
    
    # Forward signals to child process
    def sig_handler(signum, frame):
        proc.send_signal(signum)
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, sig_handler)
        
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        returncode = proc.wait()
    finally:
        stop_event.set()
        # Resume in case it was left stopped
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
            
    sys.exit(returncode)

if __name__ == "__main__":
    main()
