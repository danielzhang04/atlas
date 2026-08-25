$ErrorActionPreference = "Stop"
$script:Failed = $false

function Write-Check([string]$Status, [string]$Message) {
    Write-Output "[$Status] $Message"
    if ($Status -eq "FAIL") { $script:Failed = $true }
}

function Test-UnderRoot([string]$Path, [string]$Root) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    try {
        $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
        $base = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
        return $candidate.Equals($base, [System.StringComparison]::OrdinalIgnoreCase) -or
            $candidate.StartsWith($base + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$deployedRoot = "C:\Users\danie\Atlas"
if ($repoRoot.Equals($deployedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Check "OK" "Canonical checkout root"
} else {
    Write-Check "FAIL" "Checkout root is not C:\Users\danie\Atlas"
}

$venv = Join-Path $repoRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (Test-Path -LiteralPath $venv -PathType Container) { Write-Check "OK" "Adjacent .venv exists" }
else { Write-Check "FAIL" "Adjacent .venv is missing" }
if (Test-Path -LiteralPath $python -PathType Leaf) {
    & $python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) { Write-Check "OK" "Virtual environment uses Python 3.13" }
    else { Write-Check "FAIL" "Virtual environment does not use Python 3.13" }
} else { Write-Check "FAIL" "Virtual environment Python is missing" }

$moduleNames = @{
    "pyyaml" = "yaml"; "livekit-agents" = "livekit.agents"
    "livekit-plugins-deepgram" = "livekit.plugins.deepgram"
    "livekit-plugins-elevenlabs" = "livekit.plugins.elevenlabs"
    "livekit-plugins-silero" = "livekit.plugins.silero"
    "pywebview" = "webview"
}
$requirements = Get-Content -LiteralPath (Join-Path $repoRoot "requirements.txt")
foreach ($line in $requirements) {
    if ($line -notmatch '^\s*([A-Za-z0-9_.-]+)') { continue }
    $package = $Matches[1].ToLowerInvariant()
    $module = $moduleNames[$package]
    if (-not $module) { $module = $package.Replace("-", "_") }
    if (Test-Path -LiteralPath $python -PathType Leaf) {
        & $python -c "import $module" *> $null
        if ($LASTEXITCODE -eq 0) { Write-Check "OK" "Runtime dependency import: $module" }
        else { Write-Check "FAIL" "Runtime dependency import failed: $module" }
    } else { Write-Check "FAIL" "Runtime dependency import unavailable: $module" }
}

$configPath = Join-Path $repoRoot "config\atlas.yaml"
$configResult = $null
if (Test-Path -LiteralPath $python -PathType Leaf) {
    $parse = "import json,pathlib,sys,yaml; data=yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); keys=('max_tokens','turn_timeout_s','file_roots','state_port'); mapping=isinstance(data,dict); port=data.get('state_port') if mapping else None; print(json.dumps({'is_mapping':mapping,'required':{key:key in data for key in keys} if mapping else {},'state_port':port if type(port) is int else None}))"
    $configJson = & $python -c $parse $configPath 2> $null
    if ($LASTEXITCODE -eq 0) {
        try { $configResult = $configJson | ConvertFrom-Json } catch { $configResult = $null }
    }
    if ($configResult -and $configResult.is_mapping) {
        Write-Check "OK" "config/atlas.yaml parses as a mapping"
    } else { Write-Check "FAIL" "config/atlas.yaml does not parse as a mapping" }
} else { Write-Check "FAIL" "config/atlas.yaml parse unavailable without venv Python" }
$requiredConfigKeys = @("max_tokens", "turn_timeout_s", "file_roots", "state_port")
foreach ($key in $requiredConfigKeys) {
    $property = if ($configResult -and $configResult.required) {
        $configResult.required.PSObject.Properties[$key]
    } else { $null }
    if ($property -and $property.Value) { Write-Check "OK" "Required config key present: $key" }
    else { Write-Check "FAIL" "Required config key missing: $key" }
}

foreach ($asset in @("ui\index.html", "ui\app.js", "ui\styles.css")) {
    if (Test-Path -LiteralPath (Join-Path $repoRoot $asset) -PathType Leaf) { Write-Check "OK" "$asset exists" }
    else { Write-Check "FAIL" "$asset is missing" }
}
$node = Get-Command "node" -CommandType Application -ErrorAction SilentlyContinue
if ($node) {
    & $node.Source --check (Join-Path $repoRoot "ui\app.js") *> $null
    if ($LASTEXITCODE -eq 0) { Write-Check "OK" "node --check ui/app.js" }
    else { Write-Check "FAIL" "node --check ui/app.js failed" }
} else { Write-Check "WARN" "Node.js is not on PATH; ui/app.js syntax was not checked" }

$statePort = if ($configResult) { $configResult.state_port } else { $null }
if ($null -eq $statePort) {
    Write-Check "FAIL" "state_port missing from config/atlas.yaml"
} else {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback, [int]$statePort)
        $listener.Start()
        Write-Check "OK" "State port $statePort is free"
    } catch { Write-Check "FAIL" "State port $statePort is unavailable" }
    finally { if ($listener) { $listener.Stop() } }
}

if (Get-Command "claude" -ErrorAction SilentlyContinue) { Write-Check "OK" "claude executable is on PATH" }
else { Write-Check "FAIL" "claude executable is not on PATH" }

$shortcutChecks = @(
    [pscustomobject]@{
        Label = "Start menu"
        Path = Join-Path ([Environment]::GetFolderPath("Programs")) "Atlas.lnk"
    },
    [pscustomobject]@{
        Label = "Desktop"
        Path = Join-Path ([Environment]::GetFolderPath("Desktop")) "Atlas.lnk"
    }
)
$shell = $null
foreach ($shortcutCheck in $shortcutChecks) {
    $shortcutPath = $shortcutCheck.Path
    $shortcutLabel = $shortcutCheck.Label
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        Write-Check "OK" "${shortcutLabel}: Atlas shortcut is not present"
        continue
    }
    try {
        if (-not $shell) { $shell = New-Object -ComObject WScript.Shell }
        $shortcut = $shell.CreateShortcut($shortcutPath)
        if ((Test-UnderRoot $shortcut.TargetPath $repoRoot) -and
            (Test-UnderRoot $shortcut.WorkingDirectory $repoRoot)) {
            Write-Check "OK" "${shortcutLabel}: Atlas shortcut targets this checkout"
        } else { Write-Check "FAIL" "${shortcutLabel}: Atlas shortcut points outside this checkout" }
    } catch { Write-Check "FAIL" "${shortcutLabel}: Atlas shortcut could not be inspected" }
}

$probe = $null
try {
    if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is unavailable" }
    $runtimeDir = Join-Path $env:LOCALAPPDATA "Atlas"
    [System.IO.Directory]::CreateDirectory($runtimeDir) | Out-Null
    $probe = Join-Path $runtimeDir ([System.IO.Path]::GetRandomFileName())
    [System.IO.File]::WriteAllText($probe, "ok")
    Write-Check "OK" "Local Atlas runtime directory is writable"
} catch { Write-Check "FAIL" "Local Atlas runtime directory is not writable" }
finally { if ($probe -and (Test-Path -LiteralPath $probe)) { Remove-Item -LiteralPath $probe -Force } }

if ($script:Failed) { exit 1 }
exit 0
