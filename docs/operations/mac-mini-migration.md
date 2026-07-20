# Mac Mini Migration Guide / Setup

## Architecture

```
┌─────────────────────┐     ┌──────────────────────┐
│    MacBook Air      │     │    Mac Mini           │
│  (開發機)            │     │  (myllin_mini)        │
│                     │     │                       │
│  Tailscale:         │     │  Tailscale:            │
│  100.81.38.22       │◄────│  100.98.237.43         │
│                     │     │                       │
│  Browser →          │     │  PM2 managed:          │
│  http://100.98      │     │  ├─ trading-system     │
│  .237.43:8500       │     │  ├─ dashboard (:8500)  │
│  SSH →              │     │  └─ (auto-restart)     │
│  ssh myllin_mini    │     │                       │
│    @100.98.237.43   │     │  Hermes Agent          │
│                     │     │  Crontab (data refresh)│
└─────────────────────┘     └──────────────────────┘
```

> 所有連線透過 Tailscale 加密通道，不受 DHCP、手機熱點或 VLAN 變動影響。
> IP 以實際 `tailscale status` 輸出為準。

## Quick Install

On a brand new Mac Mini (after Tailscale is set up on both machines):

```bash
# 從舊 Mac 複製 repo
rsync -avz --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='.git/objects' \
  /Users/mylin/Documents/mylin102/tw-trading-unified/ \
  myllin_mini@100.98.237.43:~/Documents/mylin102/tw-trading-unified/

# SSH 到 Mac Mini
ssh myllin_mini@100.98.237.43

# 執行安裝腳本
cd ~/Documents/mylin102/tw-trading-unified
chmod +x scripts/mac-mini-setup.sh
./scripts/mac-mini-setup.sh
```

Then edit `~/.hermes/.env` with the Shioaji API key.

## Prerequisites

- Two Macs with Tailscale installed (`brew install --cask tailscale`)
- macOS 24+ (Apple Silicon)
- Shioaji API credentials (in `.env`)
- ~5GB free disk on Mac Mini

## Migration From Old Mac (full steps)

| Step | Command | Est. Time |
|------|---------|-----------|
| 1. Install Tailscale | `brew install --cask tailscale` (both machines) | 5m |
| 2. Auth Tailscale | `tailscale up` → open URL in browser | 1m |
| 3. Get Tailscale IP | `tailscale status` | 5s |
| 4. Run setup | `./scripts/mac-mini-setup.sh` on Mac Mini | 10-15m |
| 5. Copy Hermes | `rsync -avz ~/.hermes/ myllin_mini@[TS-IP]:~/.hermes/` | 5m |
| 6. Copy sessions | `tar czf - -C ~/.hermes sessions \| ssh myllin_mini@[TS-IP] 'tar xzf - -C ~/.hermes'` | 2m |
| 7. Copy agy | `scp ~/.local/bin/agy myllin_mini@[TS-IP]:~/.local/bin/` | 10s |
| 8. Copy .env | `scp .env myllin_mini@[TS-IP]:~/Documents/mylin102/tw-trading-unified/` | 5s |
| 9. Update crontab | `crontab -l \| ssh myllin_mini@[TS-IP] 'crontab -'` | 10s |
| 10. Start PM2 | `ssh myllin_mini@[TS-IP] 'cd ~/... && pm2 start ecosystem.config.cjs'` | 30s |
| 11. Verify | `curl http://[TS-IP]:8500` | 5s |
| **Total** | | **~20-25m** |

## Known Issues

### TXO contract date mismatch (non-fatal)

Shioaji V2 may return `delivery_date` as `datetime.date` instead of `str`.
The system handles this gracefully (`⚠️` warnings only), but the warnings are verbose.
Full fix pending: replace all `strptime()` calls with `_normalize_contract_date()`.

### Session date staleness (I-001)

`self._session_date` is computed once at `OrderManager.__init__()`.
Across 15:00 TAIFEX trading-day boundaries, the date becomes stale.
Fix tracked in: `fix/taifex-trading-day-provenance`

## Daily Operations

```bash
# SSH via Tailscale (stable IP, no reverse tunnel needed)
ssh myllin_mini@100.98.237.43

# Open Dashboard in browser
open http://100.98.237.43:8500

# Check PM2 status
pm2 list

# View trading logs
pm2 logs trading-system --lines 50

# View dashboard logs
pm2 logs dashboard --lines 20

# Restart trading system
pm2 restart trading-system

# Check broker snapshot (read-only)
cat /tmp/mts_broker_snapshot_request.json
```

## File Inventory

| Path | Purpose |
|------|---------|
| `~/.hermes/` | Agent config, skills, sessions |
| `~/.local/bin/agy` | agy (Gemini CLI) |
| `~/Documents/mylin102/tw-trading-unified/` | Repository root |
| `~/Documents/mylin102/squeeze-backtest/` | Related backtesting repo |
| `~/.pm2/dump.pm2` | Saved PM2 process list |
| `~/Library/LaunchAgents/pm2.*.plist` | Auto-start on boot |
| `/etc/sudoers.d/pm2` | Passwordless PM2 startup |
