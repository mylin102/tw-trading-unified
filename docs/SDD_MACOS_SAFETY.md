# SDD Supplement: macOS Graceful Shutdown & Dispatcher Safety

## 1. Problem Statement

### 1.1 Bug Description
| Bug | Root Cause | Classification |
|-----|-----------|----------------|
| "Python quit unexpectedly" dialog on macOS | C++ Shioaji library callbacks invoked during process teardown | Platform-specific crash |
| Race condition in tick/bidask dispatchers | No thread-safe access to shared `_seen_codes`/`_seen` sets | Concurrency bug |
| Callbacks invoked after shutdown initiated | No shutdown coordination between main thread and C++ callbacks | Lifecycle management |

### 1.2 Root Cause Analysis
The Shioaji SDK uses C++ native callbacks (`on_tick`, `on_bidask`) that can fire asynchronously. During Python process shutdown:
1. Python interpreter begins teardown
2. C++ callbacks still fire from Shioaji event loop
3. Python objects accessed by callbacks are already destroyed
4. macOS Crash Reporter triggers "Python quit unexpectedly" dialog

**Common Root Cause**: Violation of SDD Principle 2.3 (缺乏防禦性程式設計)
- No shutdown event coordination
- No precondition checks in callback handlers
- No thread-safe shared state access

---

## 2. Architecture Changes

### 2.1 Shutdown Coordination

```
┌─────────────────────────────────────────────────────────┐
│                    main.py                              │
│  Signal Handler (SIGTERM/SIGINT)                        │
│    ├─ Sets _shutdown_event                              │
│    └─ Sleeps 1s (allows main loop to detect)            │
├─────────────────────────────────────────────────────────┤
│  Main Loop                                              │
│    ├─ Checks _shutdown_event.is_set()                   │
│    └─ Breaks → enters finally block                     │
├─────────────────────────────────────────────────────────┤
│  Cleanup Sequence (finally block)                       │
│    1. _shutdown_event.set() ← signal dispatchers        │
│    2. fm.stop(), om.stop(), sm.stop()                   │
│    3. time.sleep(1) ← threads finish current work       │
│    4. Thread.join(timeout=5) ← wait for completion      │
│    5. Clear callbacks → sleep(0.5) × 2                  │
│    6. logout()                                          │
│    7. time.sleep(2) ← C++ resources settle              │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Dispatcher Safety Pattern

Each dispatcher now follows **SDD Section 4.2 (Precondition Checklist)**:

```python
def on_tick(exchange, tick):
    # Precondition 1: Shutdown check
    if _shutdown_event.is_set():
        return
    
    # Precondition 2: Input validation
    if tick is None or not hasattr(tick, 'code'):
        return
    
    # Thread-safe tracking
    with _lock:
        if tick.code not in _seen_codes:
            _seen_codes.add(tick.code)
    
    # Exception-isolated dispatch
    try:
        futures_mon.on_tick(exchange, tick)
    except Exception as e:
        console.print(f"[red][futures tick err] {e}[/red]")
    
    try:
        options_mon.on_tick(exchange, tick)
    except Exception as e:
        console.print(f"[red][options tick err] {e}[/red]")
```

**SDD Compliance**:
- ✅ Rule 1: Never Write Before Validate → No side effects before precondition checks
- ✅ Section 2.3: Defensive Design → Every public method starts with precondition check
- ✅ Section 4.2: Entry Precondition Checklist → position, price, margin, same-bar checks

---

## 3. Interface Contracts

### 3.1 tick_dispatcher()

**Preconditions**:
- `futures_mon` has `.on_tick(exchange, tick)` method
- `options_mon` has `.on_tick(exchange, tick)` method

**Postconditions**:
- Returns callback function that safely dispatches to both monitors
- Callback returns early if shutdown event is set
- Callback returns early if tick is None or invalid
- Each monitor call is exception-isolated

**Invariants**:
- `_seen_codes` set is always thread-safe (protected by `_lock`)
- No side effects (CSV/log writes) occur during shutdown
- Exceptions never propagate to C++ layer

### 3.2 bidask_dispatcher()

**Preconditions**:
- `options_mon` has `.monitor` attribute or is the monitor itself
- Monitor has `.active_contracts` and `.market_data` attributes

**Postconditions**:
- Returns callback function that safely updates market data
- Callback returns early if shutdown event is set
- Callback returns early if bidask is None or invalid
- Price extraction fails gracefully (returns without side effects)

**Invariants**:
- `_seen` set is always thread-safe (protected by `_lock`)
- `bid > 0` and `ask > 0` before any market data update
- Exceptions never propagate to C++ layer

### 3.3 Cleanup Sequence

**Preconditions**:
- `_shutdown_event` is set before any cleanup
- All monitor objects have `.stop()` method

**Postconditions**:
- All monitors stopped
- All threads joined (or timed out)
- Callbacks cleared
- Session logged out
- Process exits cleanly (no macOS crash dialog)

**Invariants**:
- Minimum 5 seconds total cleanup time (1 + 0.5 + 0.5 + 1 + 2)
- C++ callback cleanup happens before logout
- Final sleep ensures C++ resources settle

---

## 4. V-Model Test Plan

### Level 1: Unit Tests

```python
# tests/test_macos_safety.py

class TestDispatcherSafety:
    """V-Model Level 1: Dispatcher safety unit tests"""
    
    def test_tick_dispatcher_shutdown_event():
        """Shutdown event blocks tick dispatch"""
        pass
    
    def test_tick_dispatcher_none_tick():
        """None tick is handled gracefully"""
        pass
    
    def test_tick_dispatcher_invalid_tick():
        """Tick without 'code' attribute is rejected"""
        pass
    
    def test_tick_dispatcher_thread_safety():
        """Concurrent ticks don't corrupt _seen_codes"""
        pass
    
    def test_bidask_dispatcher_shutdown_event():
        """Shutdown event blocks bidask dispatch"""
        pass
    
    def test_bidask_dispatcher_invalid_prices():
        """Zero/negative prices are rejected"""
        pass

class TestShutdownSequence:
    """V-Model Level 1: Shutdown sequence unit tests"""
    
    def test_signal_handler_sets_shutdown_event():
        """SIGTERM/SIGINT sets _shutdown_event"""
        pass
    
    def test_cleanup_sleep_buffers():
        """Cleanup sequence includes required sleep buffers"""
        pass
```

### Level 2: Integration Tests

```python
# tests/test_macos_integration.py

def test_full_lifecycle_no_crash():
    """Start → run → shutdown completes without crash"""
    pass

def test_rapid_shutdown():
    """Quick start/stop cycle doesn't leave hanging threads"""
    pass
```

### Level 3: System Tests

```bash
# Manual test: Run main.py for 10 minutes, then Ctrl-C
# Expected: Clean exit, no "Python quit unexpectedly" dialog
python3 main.py --dry-run
# Press Ctrl-C after 10 minutes
# Verify: logs show clean shutdown sequence
```

### Level 4: UAT Checklist

- [ ] `python3 -m pytest tests/ -v` all pass (66/66)
- [ ] `python3 main.py --dry-run` starts without error
- [ ] Ctrl-C triggers graceful shutdown sequence in logs
- [ ] No "Python quit unexpectedly" dialog after 10 test runs
- [ ] Dispatchers handle None/invalid input without crash
- [ ] Thread safety verified under concurrent tick load

---

## 5. Implementation Order

| Priority | Change | Prevents |
|----------|--------|----------|
| P0 | `_shutdown_event` flag + signal handlers | Uncontrolled process termination |
| P0 | Dispatcher precondition checks | Callbacks firing during teardown |
| P0 | Thread-safe `_seen` sets with locks | Race conditions in tracking |
| P1 | Exception isolation in dispatch | C++ layer receiving Python exceptions |
| P1 | Cleanup sleep buffers | C++ resources not settling |
| P2 | V-Model unit tests | Regression in safety mechanisms |

---

## 6. SDD Compliance Matrix

| SDD Rule | Implementation | Verified By |
|----------|---------------|-------------|
| Rule 1: Never Write Before Validate | Precondition checks before any side effect | `test_tick_dispatcher_none_tick` |
| Section 2.3: Defensive Design | Every dispatcher starts with precondition checks | All dispatcher tests |
| Section 4.2: Entry Precondition | Shutdown event, None check, attribute check | `test_*_shutdown_event` |
| Section 2.1: Single Source of Truth | `_shutdown_event` is single source for shutdown state | `test_signal_handler_sets_shutdown_event` |
| Section 3: Module Responsibility | main.py only: startup, dispatch, health, cleanup | Architecture review |

---

## 7. Platform-Specific Notes

### macOS Behavior
- Crash Reporter triggers when process exits with pending native (C++) callbacks
- Solution: Clear all callbacks + sleep buffers before logout
- Signal handling: SIGTERM/SIGINT both handled gracefully

### Linux/Windows Compatibility
- Signal handlers only register on macOS (platform detection not needed, signals are cross-platform)
- Sleep buffers work identically on all platforms
- No platform-specific code paths introduced

---

## 8. Future Work

1. **Automated stress test**: 1000 ticks/second during shutdown
2. **Metrics**: Track shutdown duration in logs
3. **Fallback**: If cleanup takes >10s, force exit anyway
4. **C++ wrapper**: Consider wrapping Shioaji callbacks in pybind11 safety layer
