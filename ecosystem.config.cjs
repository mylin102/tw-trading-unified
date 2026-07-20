module.exports = {
  apps: [
    {
      name: "trading-system",
      cwd: "/Users/myllin_mini/Documents/mylin102/tw-trading-unified",
      script: "/Users/myllin_mini/Documents/mylin102/tw-trading-unified/.venv/bin/python",
      args: "main.py",
      interpreter: "none",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 5,
      min_uptime: "30s",
      kill_timeout: 15000,
      time: true,
      out_file: "logs/pm2-trading-out.log",
      error_file: "logs/pm2-trading-error.log",
      env: {
        PYTHONUNBUFFERED: "1"
      }
    },
    {
      name: "dashboard",
      cwd: "/Users/myllin_mini/Documents/mylin102/tw-trading-unified",
      script: "/Users/myllin_mini/Documents/mylin102/tw-trading-unified/.venv/bin/streamlit",
      args: "run ui/dashboard.py --server.port 8500 --server.headless=true --server.address 0.0.0.0",
      interpreter: "none",
      autorestart: true,
      restart_delay: 3000,
      max_restarts: 10,
      min_uptime: "20s",
      time: true,
      out_file: "logs/pm2-dashboard-out.log",
      error_file: "logs/pm2-dashboard-error.log"
    }
  ]
};
