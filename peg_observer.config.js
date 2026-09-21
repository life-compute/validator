// PM2 app definition — LIFE Compute pegRNA observer (Stage 4, LOG-ONLY).
//
// DELIBERATELY A SEPARATE FILE from ecosystem.config.js.
//
// Registering this app by editing ecosystem.config.js would be enough to
// tempt a `pm2 restart` / `pm2 reload` of the whole file, which would bounce
// life-validator — interrupting in-flight Boltz2 validations (~60 s each) and
// risking GpuBiasTracker in-memory state, whose _save() rewrites the entire
// bias file from memory on every record(). Keeping the definition in its own
// file makes it impossible to restart the validator by accident:
//
//     pm2 start peg_observer.ecosystem.cjs      # starts ONLY this app
//     pm2 stop  life-peg-observer
//     pm2 logs  life-peg-observer
//
// This process performs NO on-chain calls, consumes NO rate-limit slots,
// emits NO CONFIRM/REJECT, and shares no state with life-validator.
module.exports = {
  apps: [
    {
      name: 'life-peg-observer',
      script: 'peg_observer.py',
      args: '--watch',
      interpreter: 'python3',
      cwd: __dirname,
      env: {
        // Point this at wherever the miner's log lands: a synced directory,
        // an rsync target, or a git-fetched checkout of the miner branch.
        PEG_MINER_LOG:    __dirname + '/peg_sync/life_peg_scores.jsonl',
        PEG_SPEC_PATH:    __dirname + '/peg_vendor/peg_spec.json',
        PEG_MODEL_PATH:   __dirname + '/peg_vendor/peg_model.json',
        PEG_POLL_SECONDS: '60',
      },
      autorestart: true,
      max_restarts: 20,
      exp_backoff_restart_delay: 5000,
      watch: false,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
    },
  ],
};
