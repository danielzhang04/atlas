// pm2-native shim for the Atlas voice worker (always-on; conversation-rules wave 2026-07-21).
// pm2 on Windows can't pass `-m worker.app console` to python.exe or run a .cmd (it node-requires
// it), so pm2 runs THIS file: it spawns the venv python and mirrors its exit code, so pm2's
// crash-restart applies to the real worker. PYTHONUTF8: the livekit banner emoji dies on cp1252
// when stdout is piped into pm2 logs.
const { spawn } = require("child_process");
const path = require("path");

const child = spawn(
  path.join(__dirname, ".venv", "Scripts", "python.exe"),
  ["-m", "worker.app", "console"],
  { cwd: __dirname, stdio: "inherit", env: { ...process.env, PYTHONUTF8: "1" } },
);
// Review 2026-07-22: a spawn failure (e.g. wrong venv path) must log clearly and exit nonzero,
// not throw unhandled into a silent pm2 fast-restart loop.
child.on("error", (err) => {
  console.error("run-worker: failed to spawn venv python:", err.message);
  process.exit(1);
});
// Forward stop signals so pm2 stop/restart reaches the python worker directly rather than
// relying solely on pm2's tree-kill.
for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, () => {
    try { child.kill(sig); } catch (_) { /* already gone */ }
  });
}
child.on("exit", (code, signal) => process.exit(code === null ? 1 : code));
