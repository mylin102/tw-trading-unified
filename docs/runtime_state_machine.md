# Runtime State Machine Visualization

This document defines the lifecycle of the trading system. 

```mermaid
stateDiagram-v2
    [*] --> BOOTING: Startup
    BOOTING --> SYNCING: Shioaji Login / Contract Fetch
    SYNCING --> WARMUP: Historical Data Backfill
    WARMUP --> TRADING_READY: Indicators Stabilized
    
    TRADING_READY --> DEGRADED: Feed Stale (>120s)
    DEGRADED --> RECOVERY: Attempting Re-subscription
    RECOVERY --> TRADING_READY: Feed Fresh
    RECOVERY --> HALTED: Recovery Failed
    
    TRADING_READY --> HALTED: Risk Limit / Manual Stop
    HALTED --> SHUTDOWN: Cleanup
    SHUTDOWN --> [*]
    
    state TRADING_READY {
        [*] --> ROUTE_SIGNAL_ENTER
        ROUTE_SIGNAL_ENTER --> STRATEGY_EVAL
        STRATEGY_EVAL --> ROUTER_DECISION
        ROUTER_DECISION --> ORDER_SUBMIT: TRADE
        ROUTER_DECISION --> WAIT_NEXT_BAR: NO_ENTRY
        ORDER_SUBMIT --> WAIT_NEXT_BAR
    }
```

## State Definitions

1. **BOOTING**: Loading configuration, initializing singletons.
2. **SYNCING**: Authenticating with broker and resolving contract metadata.
3. **WARMUP**: Backfilling OHLCV data and calculating initial indicators.
4. **TRADING_READY**: All systems green, evaluating strategies on every bar.
5. **DEGRADED**: Ingestion delay detected. Trading paused for safety.
6. **RECOVERY**: Automated attempts to restore data stream.
7. **HALTED**: System stopped due to critical error or user command.
8. **SHUTDOWN**: Clearing memory, logging final PnL, closing connections.
