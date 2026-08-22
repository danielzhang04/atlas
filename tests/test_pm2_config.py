"""Shape guard for the single Atlas PM2 worker."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

ATLAS = Path(__file__).resolve().parents[1]
CONFIG = ATLAS / "pm2.config.cjs"


def test_atlas_pm2_config_has_one_safe_worker():
    result = subprocess.run(
        ["node", "-e", "process.stdout.write(JSON.stringify(require(process.argv[1])))", str(CONFIG)],
        check=True,
        capture_output=True,
        text=True,
    )
    config = json.loads(result.stdout)
    assert list(config) == ["apps"]
    assert len(config["apps"]) == 1
    app = config["apps"][0]
    assert app == {
        "name": "atlas-worker",
        "script": "run-worker.js",
        "interpreter": "node",
        "cwd": str(ATLAS),
        "instances": 1,
        "exec_mode": "fork",
        "autorestart": True,
        "min_uptime": 30_000,
        "max_restarts": 10,
        "restart_delay": 5_000,
        "kill_timeout": 15_000,
    }
