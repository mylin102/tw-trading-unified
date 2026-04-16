module.exports = {
  apps: [
    {
      name: "trading-system",
      script: "main.py",
      interpreter: "python3",
      cwd: "/Users/mylin/Documents/mylin102/tw-trading-unified",
      env: {
        PYTHONPATH: "/Users/mylin/Documents/mylin102/tw-trading-unified",
        NODE_ENV: "production"
      },
      instances: 1,
      exec_mode: "fork",
      max_memory_restart: "1G",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 3000,
      exp_backoff_restart_delay: 100,
      min_uptime: "10s",
      kill_timeout: 5000,
      listen_timeout: 3000,
      error_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-trading-error.log",
      out_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-trading-out.log",
      log_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-trading-combined.log",
      pid_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-trading.pid"
    },
    {
      name: "trading-dashboard",
      script: "ui/dashboard.py",
      interpreter: "python3",
      cwd: "/Users/mylin/Documents/mylin102/tw-trading-unified",
      args: [
        "--server.port", "8500",
        "--server.address", "127.0.0.1",
        "--server.headless", "true"
      ],
      env: {
        PYTHONPATH: "/Users/mylin/Documents/mylin102/tw-trading-unified",
        NODE_ENV: "production"
      },
      instances: 1,
      exec_mode: "fork",
      max_memory_restart: "500M",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 2000,
      exp_backoff_restart_delay: 100,
      min_uptime: "5s",
      kill_timeout: 3000,
      listen_timeout: 2000,
      error_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-dashboard-error.log",
      out_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-dashboard-out.log",
      log_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-dashboard-combined.log",
      pid_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-dashboard.pid"
    },
    {
      name: "backtest-dashboard",
      script: "ui/backtest_dashboard.py",
      interpreter: "python3",
      cwd: "/Users/mylin/Documents/mylin102/tw-trading-unified",
      args: [
        "--server.port", "8501",
        "--server.address", "127.0.0.1",
        "--server.headless", "true"
      ],
      env: {
        PYTHONPATH: "/Users/mylin/Documents/mylin102/tw-trading-unified",
        NODE_ENV: "production"
      },
      instances: 1,
      exec_mode: "fork",
      max_memory_restart: "500M",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 2000,
      exp_backoff_restart_delay: 100,
      min_uptime: "5s",
      kill_timeout: 3000,
      listen_timeout: 2000,
      error_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-backtest-error.log",
      out_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-backtest-out.log",
      log_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-backtest-combined.log",
      pid_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-backtest.pid"
    }
  ]
};