/**
 * Always-on supervision for the Atlas voice worker.
 * Credentials are loaded by the worker and never belong in this file.
 */
module.exports = {
  apps: [
    {
      name: 'atlas-worker',
      script: 'run-worker.js',
      interpreter: 'node',
      cwd: __dirname,
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      min_uptime: 30_000,
      max_restarts: 10,
      restart_delay: 5_000,
      kill_timeout: 15_000,
    },
  ],
};
