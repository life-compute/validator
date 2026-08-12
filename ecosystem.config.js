// PM2 ecosystem — LIFE Compute Validator (devnet)
module.exports = {
  apps: [
    {
      name: 'life-validator',
      script: 'validator_daemon.py',
      interpreter: 'python3',
      cwd: __dirname,
      env_file: '.env',
      autorestart: true,
      max_restarts: 20,
      exp_backoff_restart_delay: 5000,
      watch: false,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
    },
    {
      name: 'life-validator-dashboard',
      script: 'dashboard/server.cjs',
      interpreter: 'node',
      cwd: __dirname,
      env: { DASHBOARD_PORT: '3002' },
      autorestart: true,
      max_restarts: 10,
      watch: false,
    },
  ],
};
