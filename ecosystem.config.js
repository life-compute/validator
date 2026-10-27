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
    // The 'life-crank' app was REMOVED with the coordinator migration (C2).
    // The coordinator's payout batches mint LIFE now; confirming on-chain
    // never did.  life_crank.js, output/crank_queue.jsonl and the
    // append_crank_queue writer are all gone.
  ],
};
