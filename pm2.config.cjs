/**
 * Always-on PM2 supervision for the Atlas desk voice worker.
 *
 * `run-worker.js` is the Windows-safe shim that starts the Atlas virtualenv's
 * `python.exe -m worker.app console`, forwards stop signals, and mirrors the
 * worker's exit status so PM2 can supervise it.
 *
 * CREDENTIAL RULE: this committed file contains no environment values or
 * secrets. Atlas reads its user-private runtime configuration through the
 * existing worker path; do not add credentials here.
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
      // Stop retrying after ten consecutive failures before the worker has
      // stayed healthy for 30 seconds; pause between retries to avoid a hot loop.
      min_uptime: 30_000,
      max_restarts: 10,
      restart_delay: 5_000,
      // Give the shim time to forward PM2's stop signal and let Python shut down.
      kill_timeout: 15_000,
    },
    {
      name: 'atlas-subscription',
      script: 'run-subscription-worker.js',
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
