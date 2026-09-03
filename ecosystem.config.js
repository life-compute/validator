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
    {
      // Permissionless mint_reward crank.
      // Reads confirmed ResultSubmission pubkeys from output/crank_queue.jsonl
      // (written by life-validator on each CONFIRM+tx) and calls mint_reward
      // for each one.  Also runs a full chain scan every 30 min as a safety net
      // to catch anything the queue missed (backlog, restarts, race conditions).
      //
      // The on-chain reward_minted flag makes every call idempotent: if the
      // crank processes the same pubkey twice, the second call is rejected by
      // the program with RewardAlreadyMinted and is silently skipped.
      //
      // rate limit: INTER_TX_DELAY_MS between mints (devnet: 2000ms = ~30/min)
      name: 'life-crank',
      script: 'life_crank.js',
      interpreter: 'node',
      cwd: __dirname,
      env: {
        LIFE_PROGRAM_ID:     '74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf',
        SOLANA_RPC:          'https://api.devnet.solana.com',
        CRANK_KEYPAIR:       '/root/.life-compute/wallet.json',
        INTER_TX_DELAY_MS:   '2000',
        SCAN_INTERVAL_MS:    '1800000',   // 30 min periodic full scan
        SCAN_BATCH_DELAY_MS: '1200',
      },
      autorestart: true,
      max_restarts: 50,
      exp_backoff_restart_delay: 5000,
      watch: false,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
    },
  ],
};
