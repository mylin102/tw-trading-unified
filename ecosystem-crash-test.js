module.exports = {
  apps: [
    {
      name: "crash-test",
      script: "test_crash.py",
      interpreter: "python3",
      cwd: "/Users/mylin/Documents/mylin102/tw-trading-unified",
      instances: 1,
      exec_mode: "fork",
      max_memory_restart: "100M",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 1000,  // 1秒後重啟
      exp_backoff_restart_delay: 100,
      min_uptime: "2s",
      kill_timeout: 3000,
      listen_timeout: 2000,
      error_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-crash-error.log",
      out_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-crash-out.log",
      log_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-crash-combined.log",
      pid_file: "/Users/mylin/Documents/mylin102/tw-trading-unified/logs/pm2-crash.pid"
    }
  ]
};