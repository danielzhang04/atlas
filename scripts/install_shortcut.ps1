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

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class AtlasShortcutProperties {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    private struct PropertyKey {
        public Guid FormatId;
        public uint PropertyId;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct PropVariant {
        [FieldOffset(0)] public ushort VariantType;
        [FieldOffset(8)] public IntPtr PointerValue;
    }

    [ComImport]
    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHGetPropertyStoreFromParsingName(
        string path, IntPtr bindContext, uint flags, ref Guid interfaceId,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore propertyStore);

    public static void SetAppUserModelId(string path, string appId) {
        // System.AppUserModel.ID is property 5 in this format identifier.
        Guid interfaceId = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref interfaceId, out store);
        var key = new PropertyKey {
            FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
            PropertyId = 5
        };
        var value = new PropVariant {
            VariantType = 31,
            PointerValue = Marshal.StringToCoTaskMemUni(appId)
        };
        try {
            Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value));
            Marshal.ThrowExceptionForHR(store.Commit());
        } finally {
            Marshal.FreeCoTaskMem(value.PointerValue);
            Marshal.ReleaseComObject(store);
        }
    }
}
"@

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
    [AtlasShortcutProperties]::SetAppUserModelId($shortcutPath, "Atlas.Desktop")
    Write-Output "Installed Atlas shortcut: $shortcutPath"
}
