<#
.SYNOPSIS
    Launches Shelly PC Screens from the sources at sign-in, without a console.
    The installed executable does not need it: it registers itself.

.DESCRIPTION
    Places a shortcut in the user's Startup folder. This folder requires no
    administrator rights, unlike a scheduled task, and is enough here: the
    app does not need to run before sign-in.

    The shortcut targets pythonw.exe directly. Going through a .cmd would
    make a console flicker, and a detour through wscript would add a process
    for nothing.

    The boot screen, for its part, does not depend on this shortcut: it stays
    powered at all times -- or is switched back on by the power strip itself
    if power-based detection is enabled -- precisely because nothing runs
    during POST and the sign-in screen.

.PARAMETER Desktop
    Also places a shortcut on the Desktop, to launch the app by hand without
    a console.

.PARAMETER Remove
    Removes the shortcuts instead of installing them.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-startup.ps1
    powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Desktop
    powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Remove
#>
param([switch]$Desktop, [switch]$Remove)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcut = Join-Path $startupDir "Shelly Screens.lnk"
$desktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "Shelly Screens.lnk"
$entryPoint = Join-Path $projectDir "main.py"
$iconFile = Join-Path $projectDir "windows-icons/icon.ico"

if ($Remove) {
    $removed = $false
    foreach ($path in @($shortcut, $desktopLink)) {
        if (Test-Path $path) {
            Remove-Item $path -Force
            Write-Host "Removed: $path" -ForegroundColor Green
            $removed = $true
        }
    }
    if (-not $removed) { Write-Host "No shortcut to remove." }
    exit 0
}

if (-not (Test-Path $entryPoint)) {
    Write-Error "Entry point not found: $entryPoint"
    exit 1
}

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    Write-Error "pythonw.exe not found in PATH: cannot start without a console."
    exit 1
}

$shell = New-Object -ComObject WScript.Shell

function New-Launcher([string]$Path) {
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $pythonw
    $link.Arguments = '"' + $entryPoint + '"'
    $link.WorkingDirectory = $projectDir
    $link.WindowStyle = 7      # minimized: no window is shown
    $link.Description = "Shelly Screens - monitor power profiles"
    if (Test-Path $iconFile) { $link.IconLocation = "$iconFile,0" }
    $link.Save()
}

New-Launcher $shortcut
if ($Desktop) {
    New-Launcher $desktopLink
    Write-Host "Desktop shortcut: $desktopLink" -ForegroundColor Green
}

Write-Host "Shortcut created: $shortcut" -ForegroundColor Green
Write-Host "Target          : $pythonw"
Write-Host "Shelly Screens will start without a console at the next sign-in."
Write-Host "Logs            : $(Join-Path $env:ProgramData 'Shelly PC Screens\logs')"
Write-Host "To remove it    : powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Remove"
