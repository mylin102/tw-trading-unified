# PM2 Debugging

## Purpose

Diagnose and recover from PM2 process failures, VPN disconnections, and data stagnation in the trading system.

---

## Common Failure: VPN Disconnect

**Symptom:** Dashboard shows "⚠️ 期貨資料停滯 10 分鐘", PM2 shows process just restarted (uptime < 5 min).

**Root cause:** Shioaji's solClient C-level TCP socket drops when VPN disconnects. The Python process cannot recover the socket — the entire process exits and PM2 restarts it.

**Log signature:**
```
SDK NOTICE solClientOS.c:6189 TCP: Could not read from socket 12,
error = Connection reset by peer (54)
```

**Recovery is automatic:**
1. PM2 restarts the process within seconds
2. Process login to Shioaji, subscribes to contracts
3. Backfill rebuilds in-memory bars from raw tick CSV
4. Ticks accumulate; next 5m boundary closes the first bar

**Expected downtime:** 30-90 seconds (until next 5m boundary).

**What's lost:** In-progress 5m bar (ticks since last boundary). Raw tick CSV survives.

---

## Quick Recovery Check

```bash
# 1. Process alive?
pm2 status | grep trading-system

# 2. Feed fresh?
grep "feed health" /Users/mylin/.pm2/logs/trading-system-out.log | tail -3

# 3. Bars building?
tail -3 logs/market_data/MXF_20260430_PAPER_indicators.csv

# 4. Router running?
grep "RouterTrace\|New Bar" /Users/mylin/.pm2/logs/trading-system-out.log | tail -3
```

If bars are current and RouterTrace appears → system is recovered. The dashboard warning will clear when fresh data arrives.

---

## PM2 Configuration

Defined in `ecosystem.config.js`:

| Setting | Value | Notes |
|---|---|---|
| `max_memory_restart` | 2G | Prevents OOM kills during warmup (336 option contracts × QuantLib) |
| `max_restarts` | 50 | Safety net for VPN flapping |
| `restart_delay` | 5000ms | Wait 5s before restart |

---

## Log Management

pm2-logrotate is installed with:

| Setting | Value |
|---|---|
| `max_size` | 10M |
| `retain` | 5 |
| `rotateInterval` | daily at midnight |

Logs are at `/Users/mylin/.pm2/logs/trading-system-{out,error}.log`.

Current size:
```bash
ls -lh /Users/mylin/.pm2/logs/trading-system-out.log
```

---

## Manual Restart

```bash
# Soft restart (preferred)
pm2 restart trading-system

# Hard restart (clear process table)
pm2 delete trading-system && pm2 start ecosystem.config.js --only trading-system

# Both processes
pm2 restart all
```

---

## Memory Monitoring

```bash
# Current RSS
ps -o pid,rss,etime -p $(pm2 pid trading-system)

# Memory trend
pm2 monit
```

Normal RSS range: 500MB-1.2GB. If consistently above 1.5GB, consider reducing option contract subscriptions.

---

## Related

- `docs/operations/no_trade_diagnosis.md` — debug no-trade scenarios
- `docs/architecture/system_overview.md` — runtime components
- `docs/architecture/data_pipeline.md` — tick ingestion and bar recovery
