param([switch]$Force)

$ErrorActionPreference = "Stop"

$atlasRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$canonicalRoot = "C:\Users\danie\Atlas"
if (-not $Force -and -not $atlasRoot.Equals(
        $canonicalRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    [Console]::Error.WriteLine(
        "Refusing to install Atlas shortcuts outside C:\Users\danie\Atlas. Pass -Force to override.")
    exit 1
}
$pythonw = Join-Path $atlasRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "Atlas virtual environment is missing pythonw.exe: $pythonw"
}

$shell = New-Object -ComObject WScript.Shell
$icon = Join-Path $atlasRoot "ui\atlas.ico"
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    throw "Atlas icon is missing: $icon"
}

$shortcutPaths = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Atlas.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Atlas.lnk")
)

foreach ($shortcutPath in $shortcutPaths) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = "-m worker.desktop"
    $shortcut.WorkingDirectory = $atlasRoot
    $shortcut.IconLocation = "$icon,0"
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Atlas"
    $shortcut.Save()
    Write-Output "Installed Atlas shortcut: $shortcutPath"
}
