# Register only Codex Task Timer shortcuts with its own executable and AppUserModelID.
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$executable = Join-Path $project 'CodexTaskTimer.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Build CodexTaskTimer.exe first.' }
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class TimerShortcutIdentity {
    [StructLayout(LayoutKind.Sequential)] public struct Key { public Guid Format; public uint Id; }
    [StructLayout(LayoutKind.Explicit, Size=24)] public struct Value {
        [FieldOffset(0)] public ushort Type;
        [FieldOffset(8)] public IntPtr Text;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface Store {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out Key key);
        [PreserveSig] int GetValue(ref Key key, out Value value);
        [PreserveSig] int SetValue(ref Key key, ref Value value);
        [PreserveSig] int Commit();
    }
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=true)]
    static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr bind, uint flags, ref Guid iid, out Store store);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode)]
    static extern void SHChangeNotify(uint eventId, uint flags, string path, IntPtr other);
    public static void Apply(string path) {
        Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        Store store;
        Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store));
        Key key = new Key { Format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Id=5 };
        Value value = new Value { Type=31, Text=Marshal.StringToCoTaskMemUni("TheNi.CodexTaskTimer") };
        try {
            Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value));
            Marshal.ThrowExceptionForHR(store.Commit());
        } finally { Marshal.FreeCoTaskMem(value.Text); Marshal.ReleaseComObject(store); }
        SHChangeNotify(0x2000, 0x5, path, IntPtr.Zero);
    }
}
'@
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = [Environment]::GetFolderPath('StartMenu')
$paths = @(
    (Join-Path $desktop 'Codex Task Timer.lnk'),
    (Join-Path $startMenu 'Programs\Codex Task Timer.lnk')
) | Select-Object -Unique
$ws = New-Object -ComObject WScript.Shell
foreach ($path in $paths) {
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $path))) { continue }
    $shortcut = $ws.CreateShortcut($path)
    $shortcut.TargetPath = $executable
    $shortcut.Arguments = ''
    $shortcut.WorkingDirectory = $project
    $shortcut.IconLocation = $executable + ',0'
    $shortcut.Description = 'Codex Task Timer'
    $shortcut.Save()
    [TimerShortcutIdentity]::Apply($path)
    Write-Output $path
}
