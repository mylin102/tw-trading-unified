module.exports = {
  apps: [
    {
      // PM2 is the sole owner of the trading core; dashboards are started by autostart.sh.
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
      max_memory_restart: "2G",
      autorestart: true,
      watch: false,
      max_restarts: 50,
      restart_delay: 3000,
      exp_backoff_restart_delay: 100,
      min_uptime: "10s",
      kill_timeout: 5000,
      listen_timeout: 3000,
      error_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-trading-error.log",
      out_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-trading-out.log",
      log_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-trading-combined.log",
      pid_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-trading.pid"
    }
  ]
};
