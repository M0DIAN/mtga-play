param(
    [long]$WindowHandle = 0,
    [Parameter(Mandatory)][int]$FromX,
    [Parameter(Mandatory)][int]$FromY,
    [Parameter(Mandatory)][int]$ToX,
    [Parameter(Mandatory)][int]$ToY,
    [ValidateRange(250,2000)][int]$DurationMs = 500,
    [switch]$Execute,
    [switch]$Hidden,
    [string]$ResultDirectory,
    [string]$TransactionId
)
$ErrorActionPreference = 'Stop'
# Narrow MTGA fallback. Use only when the current session authorizes this
# input backend under applicable tool/skill instructions. Default is no input.
$steps = 32
$path = for ($step = 0; $step -le $steps; $step++) {
    [pscustomobject]@{
        time_ms = [math]::Round($DurationMs * $step / $steps, 3)
        x = [int][math]::Round($FromX + ($ToX - $FromX) * $step / $steps)
        y = [int][math]::Round($FromY + ($ToY - $FromY) * $step / $steps)
    }
}
foreach ($point in $path) {
    if ($point.x -lt 0 -or $point.x -ge 1920 -or $point.y -lt 0 -or $point.y -ge 1080) {
        throw 'Point outside MTGA client.'
    }
}
if ($Hidden) {
    if (-not $Execute) { throw '-Hidden requires -Execute; use the ordinary no-input plan for trajectory checks.' }
    if ([IntPtr]::Size -ne 8) { throw 'This validated helper requires 64-bit PowerShell.' }
    $parsedTransactionId = [Guid]::Empty
    if (-not [Guid]::TryParse($TransactionId, [ref]$parsedTransactionId)) {
        throw '-Hidden requires the GUID ticket from prepareTimedDrag as -TransactionId.'
    }
    if (-not $ResultDirectory -or -not [IO.Path]::IsPathRooted($ResultDirectory) -or
        -not (Test-Path -LiteralPath $ResultDirectory -PathType Container)) {
        throw '-ResultDirectory must be an existing absolute directory.'
    }
    $resultRoot = (Resolve-Path -LiteralPath $ResultDirectory).ProviderPath
    $runId = [Guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $resultRoot ($runId + '.stdout.txt')
    $stderrPath = Join-Path $resultRoot ($runId + '.stderr.txt')
    $resultPath = Join-Path $resultRoot ($runId + '.result.json')
    $startedAt = [DateTime]::UtcNow.ToString('o')
    $childProcess = $null
    $childExitCode = $null
    $launchError = $null
    $timedOut = $false
    try {
        $powerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $childArgs = @('-NoProfile', '-NonInteractive', '-File', ('"' + $PSCommandPath + '"'),
            '-WindowHandle', $WindowHandle, '-FromX', $FromX, '-FromY', $FromY,
            '-ToX', $ToX, '-ToY', $ToY, '-DurationMs', $DurationMs, '-Execute')
        # Same guarded helper, without -Hidden. No policy change or foreground workaround.
        $childProcess = Start-Process -FilePath $powerShellPath -ArgumentList $childArgs -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        $null = $childProcess.Handle # Retain the process handle so ExitCode remains available after exit.
        # Let the terminal retain/poll this invocation if slow. Killing the child
        # could interrupt its finally block while the left button is held.
        $childProcess.WaitForExit()
        $childProcess.Refresh()
        if ($childProcess.HasExited) { $childExitCode = $childProcess.ExitCode }
    } catch { $launchError = ($_ | Out-String) }
    $result = [pscustomobject]@{
        transaction_id = $TransactionId
        requested_coordinates = @{ from_x = $FromX; from_y = $FromY; to_x = $ToX; to_y = $ToY }
        exit_code = $childExitCode
        stdout = $(if (Test-Path -LiteralPath $stdoutPath) { [string](Get-Content -LiteralPath $stdoutPath -Raw) } else { '' })
        stderr = $(if (Test-Path -LiteralPath $stderrPath) { [string](Get-Content -LiteralPath $stderrPath -Raw) } else { '' })
        stdout_path = $stdoutPath; stderr_path = $stderrPath; result_path = $resultPath
        pid = $(if ($childProcess) { $childProcess.Id } else { $null })
        started_at = $startedAt; finished_at = [DateTime]::UtcNow.ToString('o')
        launch_error = $launchError; timed_out = $timedOut
    }
    $resultJson = $result | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($resultPath, $resultJson, [Text.UTF8Encoding]::new($false))
    $resultJson
    if ($launchError -or $null -eq $childExitCode) { exit 1 }
    exit $childExitCode
}
if (-not $Execute) {
    [pscustomobject]@{executed=$false; duration_ms=$DurationMs; path=$path} |
        ConvertTo-Json -Depth 4
    return
}
if ([IntPtr]::Size -ne 8) { throw 'This validated helper requires 64-bit PowerShell.' }
Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
public static class MtgaTimedDrag {
    [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int L,T,R,B; }
    [StructLayout(LayoutKind.Sequential)] struct MouseInput {
        public int dx,dy; public uint data,flags,time; public UIntPtr extra;
    }
    [StructLayout(LayoutKind.Sequential)] struct Input { public uint type; public MouseInput mouse; }
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out Rect r);
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref Point p);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll",SetLastError=true)] static extern uint SendInput(uint count,Input[] inputs,int size);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
    static void Send(uint flags,int x=0,int y=0) {
        var item=new Input {mouse=new MouseInput {flags=flags,dx=x,dy=y}};
        if(SendInput(1,new[]{item},Marshal.SizeOf(typeof(Input)))!=1)
            throw new InvalidOperationException("SendInput failed: "+Marshal.GetLastWin32Error());
    }
    static void Move(int x,int y) {
        int left=GetSystemMetrics(76),top=GetSystemMetrics(77);
        int width=GetSystemMetrics(78),height=GetSystemMetrics(79);
        Send(0xE001,(int)Math.Round((x-left)*65535.0/(width-1)),
                       (int)Math.Round((y-top)*65535.0/(height-1)));
    }
    static void Guard(IntPtr h) {
        if (!IsWindow(h) || GetForegroundWindow()!=h)
            throw new InvalidOperationException("MTGA lost foreground; drag aborted.");
    }
    public static double Run(IntPtr h,int fx,int fy,int tx,int ty,int duration) {
        Guard(h);
        if ((GetAsyncKeyState(1)&0x8000)!=0)
            throw new InvalidOperationException("Left mouse is already held; refusing input.");
        Point a=new Point {X=fx,Y=fy}, b=new Point {X=tx,Y=ty};
        if (!ClientToScreen(h,ref a)||!ClientToScreen(h,ref b))
            throw new InvalidOperationException("Coordinate mapping failed.");
        Move(a.X,a.Y);
        Thread.Sleep(150);
        Guard(h);
        bool held=false;
        var timer=new Stopwatch();
        try {
            held=true;
            Send(0x0002);
            Thread.Sleep(100);
            if ((GetAsyncKeyState(1)&0x8000)==0)
                throw new InvalidOperationException("Native left-button down was not registered.");
            timer.Start();
            for(int i=1;i<=32;i++) {
                double target=duration*i/32.0;
                while(timer.Elapsed.TotalMilliseconds<target) Thread.Sleep(1);
                Guard(h);
                int x=(int)Math.Round(a.X+(b.X-a.X)*i/32.0);
                int y=(int)Math.Round(a.Y+(b.Y-a.Y)*i/32.0);
                Move(x,y);
            }
            double movementMs=timer.Elapsed.TotalMilliseconds;
            Thread.Sleep(150);
            Guard(h);
            return movementMs;
        } finally {
            if(held) Send(0x0004);
        }
    }
}
'@
$targetCandidates = @(Get-Process -Name MTGA | Where-Object {
    $_.Path -eq 'E:\MTGA\MTGA.exe' -and $_.MainWindowTitle -eq 'MTGA'
})
if ($targetCandidates.Count -ne 1) { throw 'Expected exactly one matching MTGA process.' }
$targetHandle = $targetCandidates[0].MainWindowHandle
if ($WindowHandle -ne 0 -and $targetHandle.ToInt64() -ne $WindowHandle) {
    throw 'Supplied handle does not match the current native MTGA window.'
}
[uint32]$targetProcessId = 0
[void][MtgaTimedDrag]::GetWindowThreadProcessId($targetHandle, [ref]$targetProcessId)
$targetProcess = Get-Process -Id $targetProcessId
if ($targetProcess.Path -ne 'E:\MTGA\MTGA.exe' -or $targetProcess.MainWindowTitle -ne 'MTGA') {
    throw 'Target is not the expected MTGA executable/window.'
}
$clientRect = New-Object MtgaTimedDrag+Rect
if (-not [MtgaTimedDrag]::GetClientRect($targetHandle,[ref]$clientRect) -or
    $clientRect.R -ne 1920 -or $clientRect.B -ne 1080) {
    throw 'Client geometry changed; new screenshot and calibration required.'
}
$elapsed = [MtgaTimedDrag]::Run($targetHandle,$FromX,$FromY,$ToX,$ToY,$DurationMs)
[pscustomobject]@{executed=$true; requested_movement_ms=$DurationMs; actual_movement_ms=$elapsed; steps=$steps} |
    ConvertTo-Json
