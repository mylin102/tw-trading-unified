# Restart Failure Root Cause — 2026-04-20

## Outcome

- **Dashboard** was restarted successfully and is serving on `127.0.0.1:8500`.
- **`main.py` was not restarted** because a pre-existing `main.py` process was already holding the single-instance PID lock.

## What actually blocked `main.py`

`main.py` uses `ensure_single_instance()` at startup:

- lock file: `/tmp/tw_trading_unified.pid`
- behavior: if the PID exists and the process name contains `python`, the new instance immediately exits via `os._exit(1)`

At the time of restart:

- lock file contained PID **`51488`**
- PID `51488` was still alive
- command line of PID `51488`:

```text
/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python /Users/mylin/Documents/mylin102/tw-trading-unified/main.py
```

- working directory of PID `51488`:

```text
/Users/mylin/Documents/mylin102/tw-trading-unified
```

So the newly launched `python3 main.py` did **not** reach the trading boot path. It exited at the single-instance guard.

## Direct reproduction

Running `python3 main.py` reproduced the block:

```text
2026-04-20 19:29:11.960 | WARNING  | importlib._bootstrap:_call_with_frames_removed:488 - Optional: pip install shioaji[speed] or uv add shioaji --extra speed for better performance.
🚨 [FATAL] Another main.py instance is running (PID: 51488). Exiting.
EXIT:1
```

## Why this was confusing

`logs/unified.log` simultaneously showed repeated supervisor failures such as:

- `❌期貨停`
- `核心退出 (code=1)`
- cooldown / circuit-breaker retries

That means the system had **two different truths at once**:

1. the supervisor path believed the core was crashing/retrying
2. a separate old `main.py` process was still alive and holding the PID lock

This is why a manual restart looked like it "started and vanished" without obvious output in `manual_main.log`: the new process was killed by the lock guard before it could become the active runtime.

## Additional operator error in this session

My first restart attempt used:

- detached shell mode **plus**
- shell backgrounding with `&`

That is the wrong pattern here. In `detach: true`, the command itself must be the long-running foreground process. Using `&` made the shell exit immediately and obscured whether the child actually persisted.

Correct pattern:

```bash
cd /Users/mylin/Documents/mylin102/tw-trading-unified && exec python3 main.py >> logs/manual_main.log 2>&1
```

and similarly for Streamlit.

## Evidence collected

### PID lock

```text
/tmp/tw_trading_unified.pid = 51488
```

### Lock-holder process state

```text
PID   PPID STAT  ELAPSED      TIME COMMAND
51488 2639 Ss   01:20:46  10:30.76 .../Python .../tw-trading-unified/main.py
```

### Dashboard state after proper restart

```text
HTTP/1.1 200 OK
```

## Preventive rules

### Before restarting `main.py`

1. Check `/tmp/tw_trading_unified.pid`.
2. Verify the exact PID with `ps -fp <pid>`.
3. Decide whether that PID is:
   - the real active trading core
   - a stale/hung leftover process
4. Only then stop that **specific** PID and restart.

### When launching detached processes

1. Use detached mode with the long-running command in the foreground.
2. Do **not** add shell `&` when the launcher already supports true detachment.

## Recommended hardening

1. Make `ensure_single_instance()` log more context before exit:
   - PID
   - full command line
   - cwd
   - lock file path
2. Detect stale lock-holders more strictly than `"python" in proc.name().lower()`.
3. Add a health-aware restart playbook:
   - check lock PID
   - check actual feed health
   - check whether supervisor and lock-holder disagree
4. Consider writing a separate runtime status/lock diagnostic file so operators can immediately see:
   - active PID
   - startup time
   - last heartbeat
   - last successful tick/feed timestamps

## Operational conclusion

This restart failure was **not primarily caused by the readiness/dashboard code changes**. The immediate blocker was:

> **a pre-existing `main.py` instance (PID 51488) holding the single-instance lock, causing every new `main.py` launch to exit immediately with code 1**

The dashboard restart issue in this session was a separate launcher misuse and has already been corrected.
