// Portable release-worktree PM2 config.
//
// The release candidate runs from an ISOLATED worktree that has NO
// tracked .venv and must NEVER receive PM2 log/pid files. This config:
//
//  1. REQUIRES an explicit TRADING_PYTHON_BIN in production
//     (NODE_ENV=production at `pm2 start` time) — fail-closed, no
//     guessing an interpreter from an untracked candidate-tree venv.
//     Dev/paper may keep the shared-dependency-venv fallback.
//  2. Writes trading/dashboard logs + error/pid files ONLY under the
//     explicit runtime log directory (TRADING_RUNTIME_DIR/logs or
//     TRADING_LOG_DIR) — never into the release source tree.
//  3. Validates the runtime log dir exists and is writable BEFORE PM2
//     start (config load fails otherwise).
//  4. Never prints secrets: failure messages name variables only.
//
// Production start (example — the deploy script supplies these):
//   NODE_ENV=production \
//   TRADING_PYTHON_BIN=<explicit python interpreter> \
//   TRADING_RUNTIME_DIR=<runtime root> \
//   pm2 start ecosystem.config.js

const fs = require('fs');
const path = require('path');

const PROJECT_ROOT = __dirname;
const isProduction = process.env.NODE_ENV === 'production'
  || process.env.DEPLOY_MODE === 'production';

// ── 1. interpreter: explicit TRADING_PYTHON_BIN or fail-closed ────────────
const pythonBin = process.env.TRADING_PYTHON_BIN;
// Shared dependency venv fallback (dev/paper only — the main repo's venv
// holds the dependencies the candidate tree imports).
const SHARED_VENV = '/Users/myllin_mini/Documents/mylin102/tw-trading-unified-git/.venv/bin/python3';
let pythonPath = null;
if (pythonBin) {
  pythonPath = pythonBin;
} else if (!isProduction) {
  if (fs.existsSync(SHARED_VENV)) {
    pythonPath = SHARED_VENV; // dev/paper: shared dependency venv fallback
  } else {
    const local = path.join(PROJECT_ROOT,
                            fs.existsSync(path.join(PROJECT_ROOT, '.venv'))
                              ? '.venv' : 'venv', 'bin/python3');
    if (fs.existsSync(local)) pythonPath = local;
  }
}
if (!pythonPath) {
  // fail-closed: never guess an interpreter from an untracked candidate
  // tree venv in production (no secrets printed — variable name only)
  throw new Error(
    'TRADING_PYTHON_BIN is REQUIRED in production (fail-closed); '
    + 'refusing to guess an interpreter from the candidate tree');
}
if (!fs.existsSync(pythonPath)) {
  throw new Error('TRADING_PYTHON_BIN does not exist: '
                  + (pythonBin ? 'TRADING_PYTHON_BIN' : 'fallback venv'));
}

// ── 2/3. runtime log dir: explicit, validated, never in the release tree ──
const runtimeDir = process.env.TRADING_RUNTIME_DIR
  || '/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime';
const logDir = process.env.TRADING_LOG_DIR || path.join(runtimeDir, 'logs');
if (!fs.existsSync(logDir)) {
  throw new Error('runtime log dir does not exist (fail before PM2 start): '
                  + 'set TRADING_LOG_DIR or create '
                  + path.join(runtimeDir, 'logs'));
}
try {
  fs.accessSync(logDir, fs.constants.W_OK);
} catch (e) {
  throw new Error('runtime log dir is NOT writable (fail before PM2 start): '
                  + logDir);
}

// ── 4. no secrets: env VALUES are never echoed; only variable names ───────
const baseEnv = {
  PYTHONPATH: PROJECT_ROOT,
  PYTHONUNBUFFERED: '1',
  TRADING_RUNTIME_DIR: runtimeDir,
  TRADING_LOG_DIR: logDir,
  NODE_ENV: 'production',
};
if (pythonBin) baseEnv.TRADING_PYTHON_BIN = pythonBin;
// Deploy-time release identity: the release-identity gate reads the
// literal full SHA from the process env — pass it through untouched.
if (process.env.LRC_RELEASE_SHA) {
  baseEnv.LRC_RELEASE_SHA = process.env.LRC_RELEASE_SHA;
}

const L = (app, file) => path.join(logDir, `pm2-${app}-${file}`);

module.exports = {
  apps: [
    {
      name: 'trading-system',
      script: 'taskpolicy',
      // SSOT: futures.yaml owns both TMF and MTX product definitions. A
      // comma-separated config list creates multiple monitors and is never a
      // production PM2 deployment mode.
      args: `-c background ${pythonPath} main.py --config futures`,
      interpreter: 'none',
      cwd: PROJECT_ROOT,
      restart_delay: 15000,
      autorestart: true,
      watch: false,
      max_restarts: 2,
      min_uptime: '120s',
      kill_timeout: 30000,
      error_file: L('trading', 'error.log'),
      out_file: L('trading', 'out.log'),
      log_file: L('trading', 'combined.log'),
      pid_file: L('trading', 'trading.pid'),
      env: { ...baseEnv },
    },
    {
      name: 'dashboard',
      script: 'taskpolicy',
      args: `-c background ${pythonPath} -m streamlit run ui/dashboard.py `
          + '--server.port 8500 --server.headless=true '
          + '--server.address 0.0.0.0 --server.fileWatcherType none',
      interpreter: 'none',
      cwd: PROJECT_ROOT,
      restart_delay: 5000,
      autorestart: true,
      watch: false,
      max_restarts: 10,
      kill_timeout: 10000,
      error_file: L('dashboard', 'error.log'),
      out_file: L('dashboard', 'out.log'),
      log_file: L('dashboard', 'combined.log'),
      pid_file: L('dashboard', 'dashboard.pid'),
      env: { ...baseEnv },
    },
    {
      name: 'stock-runner',
      script: 'taskpolicy',
      args: `-c background ${pythonPath} scripts/runners/stock_runner.py`,
      interpreter: 'none',
      cwd: PROJECT_ROOT,
      restart_delay: 15000,
      autorestart: true,
      watch: false,
      max_restarts: 20,
      kill_timeout: 15000,
      error_file: L('stock', 'error.log'),
      out_file: L('stock', 'out.log'),
      log_file: L('stock', 'combined.log'),
      pid_file: L('stock', 'stock.pid'),
      env: { ...baseEnv },
    },
  ],
};
