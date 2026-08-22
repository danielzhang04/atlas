// PM2 shim for Atlas's subscription-authenticated Claude Code executor.
// The worker is idle until a voice-delegated durable job exists. It refuses startup if a metered
// provider credential is inherited, and every actual Claude run is initiated by Daniel's request.
const { spawn } = require("child_process");
const path = require("path");

const child = spawn(
  path.join(__dirname, ".venv", "Scripts", "python.exe"),
  ["-m", "worker.subscription_cli", "--confirm-subscription-auth"],
  { cwd: __dirname, stdio: "inherit", env: { ...process.env, PYTHONUTF8: "1" } },
);

child.on("error", (err) => {
  console.error("run-subscription-worker: failed to spawn venv python:", err.message);
  process.exit(1);
});

for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, () => {
    try { child.kill(sig); } catch (_) { /* already gone */ }
  });
}

child.on("exit", (code) => process.exit(code === null ? 1 : code));
