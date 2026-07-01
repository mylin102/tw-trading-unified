const path = require('path');
const PROJECT_ROOT = __dirname; // 自動獲取當前專案目錄，避免寫死絕對路徑

module.exports = {
  apps: [
    {
      name: "trading-system",
      script: "taskpolicy",
      args: `-c background ${path.join(PROJECT_ROOT, "venv/bin/python3")} main.py`,
      interpreter: "none",
      cwd: PROJECT_ROOT,
      restart_delay: 15000,        // 💡 關鍵：15秒延遲重啟，對齊原設計並保護券商連線
      autorestart: true,
      watch: false,
      max_restarts: 50,
      exp_backoff_restart_delay: 100,
      min_uptime: "10s",
      kill_timeout: 30000,        // 30s graceful shutdown
      // listen_timeout removed — taskpolicy wrapper prevents PM2 readiness detection
      error_file: path.join(PROJECT_ROOT, "logs/pm2-trading-error.log"),
      out_file: path.join(PROJECT_ROOT, "logs/pm2-trading-out.log"),
      log_file: path.join(PROJECT_ROOT, "logs/pm2-trading-combined.log"),
      pid_file: path.join(PROJECT_ROOT, "logs/pm2-trading.pid"),
      env: {
        PYTHONPATH: PROJECT_ROOT, 
        PYTHONUNBUFFERED: "1",    // 💡 關鍵：解除快取，確保 pm2 logs 能即時跳出 print 訊息
        NODE_ENV: "production"
      }
    },
    {
      name: "dashboard",
      script: "taskpolicy",
      args: `-c background ${path.join(PROJECT_ROOT, "venv/bin/python3")} -m streamlit run ui/dashboard.py --server.port 8500 --server.fileWatcherType none`,
      interpreter: "none",
      cwd: PROJECT_ROOT,
      restart_delay: 5000,
      autorestart: true,
      watch: false,
      max_restarts: 10,
      kill_timeout: 10000,
      error_file: path.join(PROJECT_ROOT, "logs/pm2-dashboard-error.log"),
      out_file: path.join(PROJECT_ROOT, "logs/pm2-dashboard-out.log"),
      log_file: path.join(PROJECT_ROOT, "logs/pm2-dashboard-combined.log"),
      pid_file: path.join(PROJECT_ROOT, "logs/pm2-dashboard.pid"),
      env: {
        PYTHONPATH: PROJECT_ROOT, 
        PYTHONUNBUFFERED: "1",    // 💡 關鍵：Streamlit 輸出同步不延遲
        NODE_ENV: "production"
      }
    }
  ]
};
