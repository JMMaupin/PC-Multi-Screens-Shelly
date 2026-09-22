<#
.SYNOPSIS
    Lance Shelly Screens a l'ouverture de session, sans console.

.DESCRIPTION
    Depose un raccourci dans le dossier Demarrage de l'utilisateur. Ce dossier
    ne demande aucun droit administrateur, contrairement a une tache planifiee,
    et suffit ici : l'application n'a pas besoin de tourner avant l'ouverture
    de session.

    Le raccourci vise directement pythonw.exe. Passer par un .cmd ferait
    scintiller une console, et un detour par wscript ajouterait un processus
    pour rien.

    L'ecran de demarrage, lui, ne depend pas de ce raccourci : il reste
    alimente en permanence -- ou rallume par la multiprise elle-meme si la
    detection de consommation est en service -- justement parce que rien ne
    tourne pendant le POST et l'ecran de connexion.

.PARAMETER Desktop
    Pose en plus un raccourci sur le Bureau, pour lancer l'application a la
    main sans console.

.PARAMETER Remove
    Retire les raccourcis au lieu de les installer.

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
            Write-Host "Supprime : $path" -ForegroundColor Green
            $removed = $true
        }
    }
    if (-not $removed) { Write-Host "Aucun raccourci a supprimer." }
    exit 0
}

if (-not (Test-Path $entryPoint)) {
    Write-Error "Point d'entree introuvable : $entryPoint"
    exit 1
}

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    Write-Error "pythonw.exe est introuvable dans le PATH : impossible de demarrer sans console."
    exit 1
}

$shell = New-Object -ComObject WScript.Shell

function New-Launcher([string]$Path) {
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $pythonw
    $link.Arguments = '"' + $entryPoint + '"'
    $link.WorkingDirectory = $projectDir
    $link.WindowStyle = 7      # reduit : aucune fenetre ne s'affiche
    $link.Description = "Shelly Screens - monitor power profiles"
    if (Test-Path $iconFile) { $link.IconLocation = "$iconFile,0" }
    $link.Save()
}

New-Launcher $shortcut
if ($Desktop) {
    New-Launcher $desktopLink
    Write-Host "Raccourci Bureau : $desktopLink" -ForegroundColor Green
}

Write-Host "Raccourci cree : $shortcut" -ForegroundColor Green
Write-Host "Cible          : $pythonw"
Write-Host "Shelly Screens demarrera sans console a la prochaine ouverture de session."
Write-Host "Journaux       : $(Join-Path $projectDir 'shelly-screens.log')"
Write-Host "Pour le retirer : powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Remove"
