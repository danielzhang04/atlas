$ErrorActionPreference = "Stop"

$atlasRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path $atlasRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "Atlas virtual environment is missing pythonw.exe: $pythonw"
}

$programs = [Environment]::GetFolderPath("Programs")
$shortcutPath = Join-Path $programs "Atlas.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "-m worker.desktop"
$shortcut.WorkingDirectory = $atlasRoot
$shortcut.Description = "Open Atlas"
$shortcut.Save()

Write-Output "Installed Atlas shortcut: $shortcutPath"
