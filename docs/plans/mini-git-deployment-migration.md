# Mini Git Deployment Migration Plan

**Status:** Plan — not yet executed
**Origin:** INC-001 finding that Mini repo is an SCP skeleton (0 git objects)
**Priority:** P0 — blocks reproducible deployment, rollback, and future incident investigation
**Risk:** High — involves production runtime cutover. No in-flight MTS positions expected (state shows has_position=false).

---

## Current State

Mini's `/Users/myllin_mini/Documents/mylin102/tw-trading-unified`:

| Attribute | Value |
|---|---|
| Deployment method | SCP (not git clone/pull) |
| .git directory | Exists but 0 objects — skeleton only |
| HEAD ref | `b416bfd8` (Jul 18 "synchronize transition gate") |
| Ref objects | NOT FOUND — commit blob does not exist |
| Working tree | Contains SCP-deployed files + incremental patches |
| Runtime | PM2 dashboard running from this tree |
| Identity | `.deployment-target` exists with `deployment_id: mini` |
| State file | `/tmp/mts_position_state.json` — has_position=false |

## Risk Assessment

| Risk | Level | Mitigation |
|---|---|---|
| Position in-flight during cutover | None | has_position=false, state shows flat |
| Config mismatch after migration | Medium | Backup full config/ + .env before clone |
| Runtime credentials lost | Medium | Backup `.hermes/`, `.env`, PM2 ecosystem |
| Dashboard downtime | Low | PM2 restart takes <30s |
| Data loss (CSV, logs, exports) | Low | Keep SCP directory as read-only snapshot |
| Python venv mismatch | Low | Recreate venv from requirements.txt |

## Migration Steps

### Phase 1 — Backup (on Mini, no downtime)

```bash
# Timestamp for archive naming
TS=$(date +%Y%m%dT%H%M%S)
BACKUP_DIR=~/tw-trading-unified-scp-snapshot-$TS

# Create backup of entire SCP deployment (read-only)
cp -a ~/Documents/mylin102/tw-trading-unified $BACKUP_DIR

# Backup critical host-local files explicitly
mkdir -p ~/migration-backup-$TS
cp ~/Documents/mylin102/tw-trading-unified/.env ~/migration-backup-$TS/ 2>/dev/null || true
cp ~/Documents/mylin102/tw-trading-unified/.deployment-target ~/migration-backup-$TS/
cp ~/Documents/mylin102/tw-trading-unified/ecosystem.config.js ~/migration-backup-$TS/ 2>/dev/null || true
cp ~/Documents/mylin102/tw-trading-unified/ecosystem.config.cjs ~/migration-backup-$TS/ 2>/dev/null || true

# Backup PM2 state
pm2 describe dashboard > ~/migration-backup-$TS/pm2-dashboard.txt 2>/dev/null || true
pm2 list > ~/migration-backup-$TS/pm2-list.txt 2>/dev/null || true
crontab -l > ~/migration-backup-$TS/crontab.txt 2>/dev/null || true

echo "Backup complete: $BACKUP_DIR"
echo "Host-local config: ~/migration-backup-$TS"
```

### Phase 2 — Git clone (on Mini, existing runtime still active)

```bash
cd ~/Documents
REPO_URL=$(git -C ~/Documents/mylin102/tw-trading-unified/.git config --get remote.origin.url 2>/dev/null)
echo "Origin: $REPO_URL"

# Clone into new directory (parallel to existing SCP deployment)
git clone "$REPO_URL" tw-trading-unified-git
cd tw-trading-unified-git
```

### Phase 3 — Deploy specific commit (Detached HEAD)

```bash
# Fetch all branches/tags without merging into working tree
git fetch --all --prune

# Switch to explicit detached HEAD for approved production commit SHA
git switch --detach <APPROVED_PRODUCTION_COMMIT_SHA>

# Verify Detached HEAD status (git branch --show-current should return empty)
git branch --show-current
git rev-parse HEAD
git status --short
```


### Phase 4 — Restore host-local config

```bash
cd ~/Documents/tw-trading-unified-git

# Restore identity file (must NOT be in git)
cp ~/migration-backup-$TS/.deployment-target .

# Record production deployment manifest evidence
mkdir -p .runtime
cat << 'EOF' > .runtime/production_deployment.json
{
  "host_role": "production_trading",
  "deployed_commit": "$(git rev-parse HEAD)",
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "approved_by": "manual",
  "source_branch": "master",
  "deployment_mode": "detached_commit",
  "deployment_reason": "Approved Production Migration / Proposal P-003"
}
EOF

# Restore runtime config
cp ~/migration-backup-$TS/.env . 2>/dev/null || echo "NO_ENV"

# Restore PM2 ecosystem config
cp ~/migration-backup-$TS/ecosystem.config.js . 2>/dev/null || true
cp ~/migration-backup-$TS/ecosystem.config.cjs . 2>/dev/null || true

# Restore crontab
crontab ~/migration-backup-$TS/crontab.txt 2>/dev/null || echo "NO_CRONTAB"

# Create Python venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Verify key runtime imports
python3 -c "
import core.shioaji_session
import core.spread_loader
from strategies.plugins.futures.active.tmf_spread import TmfSpreadStrategy
print('Key imports OK')
"
```

### Phase 5 — Preflight

```bash
cd ~/Documents/tw-trading-unified-git

# Run deployment preflight
python3 scripts/deployment_preflight.py
# Expected: ALL CHECKS PASSED, deployment_id=mini

# Run unit tests
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

### Phase 6 — Runtime cutover

```bash
cd ~/Documents/tw-trading-unified-git

# 1. Stop old PM2 dashboard
pm2 stop dashboard

# 2. Start dashboard from new git checkout
pm2 start ecosystem.config.js --only dashboard 2>/dev/null || \
  .venv/bin/python3 -m streamlit run ui/dashboard.py \
    --server.port 8500 \
    --server.headless=true \
    --server.address 0.0.0.0 \
    > logs/pm2-dashboard-out.log 2>&1 &

# 3. Verify
sleep 10
grep "You can now\|載入價差資料\|ALL CHECKS PASSED" logs/pm2-dashboard-out.log | tail -5
curl -s -o /dev/null -w "%{http_code}" http://localhost:8500/
```

### Phase 7 — Preserve SCP directory

```bash
# Rename SCP directory to read-only snapshot (not delete)
mv ~/Documents/mylin102/tw-trading-unified ~/Documents/tw-trading-unified-scp-snapshot-$TS

# Symlink new git checkout to original path for backward compatibility
ln -s ~/Documents/tw-trading-unified-git ~/Documents/mylin102/tw-trading-unified
```

### Phase 8 — Verification

```text
[ ] pm2 dashboard running from ~/Documents/tw-trading-unified-git
[ ] pm2 cwd points to new git checkout
[ ] git rev-parse HEAD matches Air4 confirmed commit
[ ] git status --short = clean
[ ] .deployment-target present and valid
[ ] preflight passes
[ ] dashboard loads and shows valid data
[ ] cron tab restored and active
[ ] SCP snapshot preserved as read-only
[ ] Original .git skeleton archived
```

## Rollback Procedure

If migration fails:

```bash
pm2 stop dashboard
rm ~/Documents/mylin102/tw-trading-unified  # removes symlink
mv ~/Documents/tw-trading-unified-scp-snapshot-<TS> ~/Documents/mylin102/tw-trading-unified
pm2 start dashboard
```

No data loss — SCP snapshot retains all CSVs, logs, state files.

## After Migration

- Air4 commits go through normal git flow
- Mini runs `git pull --ff-only` for updates
- No more SCP deployment
- `.gitignore` on Air4 must already list `.deployment-target` ✓

## Execution Requirements

| Requirement | Current status |
|---|---|
| Air4 confirmed commit | Pending — current `97deacc5` is dirty |
| Mini disk space for git clone | Need to verify |
| Mini network access to github.com | Need to verify |
| Mini PM2 ecosystem config location | Need to verify |
| Mini .env exists | Need to verify |
| Mini crontab backed up | Need to verify |

## What This Plan Does NOT Cover

- Failure Mode Reproduction (separate work package)
- Consumption provenance instrumentation (separate work package)
- Historical worktree cleanup (deferred)

---

## Commit Baseline (2026-07-23)

```
19647db0  docs: finalize INC-001 and Mini Git migration plan
c54be650  feat(deployment): add multi-host identity preflight
8277e3ab  fix(data): harden ticker-scoped spread discovery and refresh
1cf1950b  fix(runtime): restore MTS risk engine and lifecycle adapter deps
ac7a8a66  test: add contract resolution tests with empty-category guard
```

**Source note:** `risk_engine.py` and `mts_lifecycle_adapter.py` in `1cf1950b`
were sourced from the Air4 pre-existing working tree (previously uncommitted).
Their behavioral equivalence to the Jul 22 Mini runtime is not claimed —
they restore import-ability of the TMFSpread strategy for the new Git checkout.

**Mini checkout:** `git checkout --detach ac7a8a66`
