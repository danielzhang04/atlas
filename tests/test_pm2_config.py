"""Shape guard for the committed Atlas PM2 ecosystem definition."""

import json
import subprocess
from pathlib import Path


ATLAS = Path(__file__).resolve().parents[1]
CONFIG = ATLAS / "pm2.config.cjs"


def _load_config() -> dict:
    result = subprocess.run(
        [
            "node",
            "-e",
            "process.stdout.write(JSON.stringify(require(process.argv[1])))",
            str(CONFIG),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_atlas_pm2_config_has_safe_durable_shape():
    config = _load_config()

    assert list(config) == ["apps"]
    apps = {app["name"]: app for app in config["apps"]}
    assert set(apps) == {"atlas-worker", "atlas-subscription"}
    assert apps["atlas-worker"]["script"] == "run-worker.js"
    assert apps["atlas-subscription"]["script"] == "run-subscription-worker.js"
    for app in apps.values():
        assert Path(app["cwd"]).resolve() == ATLAS.resolve()
        assert app["interpreter"] == "node"
        assert app["instances"] == 1
        assert app["exec_mode"] == "fork"
        assert app["autorestart"] is True
        assert app["min_uptime"] == 30_000
        assert app["max_restarts"] == 10
        assert app["restart_delay"] == 5_000
        assert app["kill_timeout"] == 15_000
        assert "env" not in app

    shim = (ATLAS / "run-subscription-worker.js").read_text(encoding="utf-8")
    assert "--confirm-subscription-auth" in shim
    assert "ANTHROPIC_API_KEY" not in shim
