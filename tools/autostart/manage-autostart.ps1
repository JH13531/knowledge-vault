# ============================================================
#  Knowledge Vault autostart manager (Windows)
#
#  -Action install    : register the logon task AND start services now
#  -Action uninstall  : stop services AND remove the logon task
#
#  Users should NOT run this by hand. Double-click the two .bat
#  launchers next to this file instead (they request admin for you).
#
#  ASCII-ONLY ON PURPOSE: these launchers are read in the OEM
#  codepage on Chinese Windows; non-ASCII bytes can corrupt them.
# ============================================================
param(
    [ValidateSet('install', 'uninstall')]
    [string]$Action = 'install'
)

$ErrorActionPreference = 'Stop'

$TaskName = 'KnowledgeVault-Autostart'
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo     = (Resolve-Path (Join-Path $Here '..\..')).Path
$Script   = Join-Path $Here 'vault-autostart.py'

if (-not (Test-Path $Script)) { throw "Not found: $Script" }

# ---- self-elevate when started without administrator ----
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output 'Requesting administrator privileges ...'
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $PSCommandPath, '-Action', $Action)
    exit
}

function Find-Python {
    $candidates = @()
    if ($env:VAULT_PYTHON) { $candidates += $env:VAULT_PYTHON }
    $candidates += (Join-Path $Repo 'tools\.venv\Scripts\python.exe')
    $candidates += 'C:\software\anaconda3\python.exe'
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-ConsolePython {
    $py = Find-Python
    if (-not $py) { return $null }
    # --stop prints to stdout; pythonw.exe would detach and hide it.
    if ($py -match 'pythonw\.exe$') { $py = $py -replace 'pythonw\.exe$', 'python.exe' }
    return $py
}

function Install-Task {
    $py = Find-Python
    if (-not $py) { throw 'Python not found. Set VAULT_PYTHON to an absolute python.exe path.' }

    $pyDir = Split-Path -Parent $py
    $pyw = Join-Path $pyDir 'pythonw.exe'
    if (-not (Test-Path $pyw)) { $pyw = $py }

    Write-Output "repo   : $Repo"
    Write-Output "python : $pyw"

    $taskAction = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Script`"" -WorkingDirectory $Repo
    $trigger    = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings   = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings -Description 'Keep knowledge-vault clip-server (127.0.0.1:7878) and feishu-bot running' -Force | Out-Null
    Write-Output "OK - registered: $TaskName"

    try {
        Start-ScheduledTask -TaskName $TaskName
        Write-Output 'OK - started now (no reboot needed)'
    } catch {
        Write-Output "WARN - registered, but start-now failed: $($_.Exception.Message)"
        Write-Output '       It will start at your next logon.'
    }
}

function Uninstall-Task {
    # Stop the services the task started (best effort; requires Python).
    $py = Get-ConsolePython
    if ($py) {
        Write-Output 'Stopping services ...'
        & $py $Script --stop
    } else {
        Write-Output 'WARN - Python not found; services were not stopped.'
        Write-Output '       Kill them manually or reboot.'
    }

    # Remove the logon task so it will not start again next time.
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "OK - removed: $TaskName"
    } else {
        Write-Output "Task '$TaskName' was not registered - nothing to remove."
    }
}

switch ($Action) {
    'install'   { Install-Task }
    'uninstall' { Uninstall-Task }
}
