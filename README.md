# Atlas

- Atlas is a standalone local voice application with a loopback command center.
- Claude handles ordinary conversation and chooses from host-registered tools.
- App opens and MCP reads run in the conversational turn.
- Mutations require a later spoken confirmation; longer work launches through `claude --bg`.
- Background output appears in Workers, and completed results remain protected at rest.
- Open the Atlas app to turn Atlas on; close its window to turn Atlas off.

## Setup

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Put voice-provider values in `%USERPROFILE%\.atlas\env`. MCP server commands, arguments, and child
environment values remain in the user's existing `~/.claude.json`; neither file belongs in this
repository.

## Open the app

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
```

Run this once; it only creates the shortcuts. Then open Atlas from the Start menu or the Desktop icon
like any app. Closing the window turns Atlas off.

Run a single streaming text turn, with or without MCP connections:

```powershell
.venv\Scripts\python -m worker.chat "pull up gmail"
.venv\Scripts\python -m worker.chat --no-mcp "hello"
```

The UI binds only to `127.0.0.1`. Its one-use pairing secret travels in the browser URL fragment,
then the page removes it and keeps the resulting bearer only in memory.

## Configuration

| File | Purpose |
|---|---|
| `config/atlas.yaml` | Model, local paths, voice, wake word, and output device |
| `config/apps.yaml` | Teachable app aliases and signed desktop profile ids |
| `config/mcp.yaml` | MCP servers and instant-versus-confirm tool policy |
| `config/intents.yaml` | Exact local dismiss, cancel, and repeat reflexes |
| `config/persona.md` | Atlas voice and character |

## Verification

The automated suite is account-free: it must not call a model, connect to the network, open a
browser or desktop app, or launch Claude.

```powershell
.venv\Scripts\python -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp
node --check ui/app.js
.venv\Scripts\python -m compileall -q worker
git diff --check
```

Live voice, account-backed MCP, desktop opening, and paid work remain explicit human verification
steps.
