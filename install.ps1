<#
.SYNOPSIS
    Installs the compound registry for the current user (no administrator rights).

.DESCRIPTION
    Double-click install.cmd, or run in a PowerShell window:

        powershell -ExecutionPolicy Bypass -File install.ps1

    The script
      1. looks for uv (the Python package manager the project uses) and offers to install it
         for the current user when it is missing;
      2. creates the project-local Python environment (.venv, .python, .uv-cache in this folder);
      3. starts the guided installation, "compound-registry setup". It asks for the data folder
         (where the registry database, its backups and the compound files are stored), the
         browser(s), the address of the ELN and the MestReNova add-on, registers the registry
         program with the browser, builds the extension (needs Node.js) and lists what is
         left to do by hand.

    Running it again is safe: every question then shows the previous choice as its default.
    To bring an installation up to date, use update.ps1 (update.cmd).

    Every option of "compound-registry setup" can be added and is passed on unchanged:

        --home PATH          the data folder (not asked then)
        --browser NAME       firefox, chrome or both
        --eln-origin URL     address of the ELN, e.g. https://eln.example.org
        --create-registry    create a NEW, EMPTY registry in the data folder without asking
        --id-prefix TEXT     letters in front of every ID of a NEW registry (permanent)
        --id-format TEMPLATE full ID template of a NEW registry (permanent)
        --mnova yes|no       install / refresh the MestReNova add-on, or leave it alone
        --skip-extension     do not build the extension
        --force              replace a browser registration of another installation
        --yes                ask nothing: take every default or previous choice
        --upgrade            ask nothing and keep everything as it is installed
        --dry-run            only print what would be done; change nothing

    Examples:
        powershell -ExecutionPolicy Bypass -File install.ps1 --dry-run
        powershell -ExecutionPolicy Bypass -File install.ps1 --home D:\CompoundRegistry --browser both
        powershell -ExecutionPolicy Bypass -File install.ps1 --yes --home D:\CompoundRegistry --create-registry --id-prefix ABC

.PARAMETER RegistryHome
    The data folder: the registry folder the browser extension should use. Same as --home.

.PARAMETER CreateRegistry
    Create a NEW, EMPTY registry in the data folder without asking, if none exists there.
    Only for a first installation: a new registry starts numbering again at 1.
    Same as --create-registry.

.PARAMETER Browser
    Firefox, Chrome or Both. Same as --browser.

.PARAMETER Force
    Replace a browser registration that belongs to another installation of this project.
    Same as --force.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$RegistryHome = "",
    [switch]$CreateRegistry,
    [string]$Browser = "",
    [switch]$Force,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SetupArguments = @()
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$UvInstallPage = "https://docs.astral.sh/uv/getting-started/installation/"
$CloseHint = "Close Firefox/Chrome and MestReNova, then run this again."

function Stop-Install([string]$Message) {
    Write-Host ""
    Write-Host "ERROR: $Message"
    exit 1
}

# Windows PowerShell 5.1 puts an argument with a space in quotes for a program, but does not
# double a backslash at its end: "D:\Lab Data\" would swallow the quote and every argument
# after it. So: no surrounding quotes (a pasted path has them), no separator at the end.
function Get-SafeArgument([string]$Value) {
    $text = $Value.Trim()
    while ($text.Length -ge 2 -and
           (($text.StartsWith('"') -and $text.EndsWith('"')) -or
            ($text.StartsWith("'") -and $text.EndsWith("'")))) {
        $text = $text.Substring(1, $text.Length - 2).Trim()
    }
    while ($text.Length -gt 3 -and ($text.EndsWith("\") -or $text.EndsWith("/"))) {
        $text = $text.Substring(0, $text.Length - 1)
    }
    $text = $text.Replace('"', '')
    if ($text -eq "") {
        return '""'
    }
    return $text
}

# Started with "powershell -File" (install.cmd does that), a quoted folder that ends in a
# backslash arrives glued to everything after it: --home "D:\Lab Data\" --yes is ONE
# argument, D:\Lab Data" --yes. The text before the quote is the value; the rest are
# arguments of their own.
function Split-GluedArgument([string]$Value) {
    $at = $Value.IndexOf('"')
    if ($at -le 0) {
        return @($Value)
    }
    $parts = @($Value.Substring(0, $at))
    foreach ($piece in ($Value.Substring($at + 1) -split '\s+')) {
        if ($piece -ne "") { $parts += $piece }
    }
    return $parts
}

function Test-Console {
    if (-not [Environment]::UserInteractive) { return $false }
    try { return -not [Console]::IsInputRedirected } catch { return $false }
}

function Test-CanAsk {
    return (-not $script:NoQuestions) -and (Test-Console)
}

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $true }
    # Where the official installer puts uv; a window opened before it ran does not see it yet.
    $folders = @()
    if ($env:XDG_BIN_HOME) { $folders += $env:XDG_BIN_HOME }
    $folders += (Join-Path $env:USERPROFILE ".local\bin")
    foreach ($folder in $folders) {
        if (Test-Path (Join-Path $folder "uv.exe")) {
            $env:Path = "$folder;$env:Path"
            return $true
        }
    }
    return $false
}

# The registry program of THIS folder that a browser keeps running: its file cannot be
# replaced while it runs.
function Get-RunningHost {
    $prefix = (Join-Path $Root ".venv") + "\"
    $found = @()
    foreach ($process in @(Get-Process -Name "compound-registry-host" -ErrorAction SilentlyContinue)) {
        $path = $null
        try { $path = $process.Path } catch { $path = $null }
        if ($path -and $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $found += $process
        }
    }
    return $found
}

# --- The arguments for "compound-registry setup" -------------------------------------------
$SetupArgs = @()
if ($RegistryHome -ne "") { $SetupArgs += @("--home", $RegistryHome) }
if ($CreateRegistry) { $SetupArgs += "--create-registry" }
if ($Browser -ne "") {
    $name = $Browser.Trim().ToLowerInvariant()
    if (@("firefox", "chrome", "both") -notcontains $name) {
        Stop-Install "Unknown browser '$Browser': use Firefox, Chrome or Both."
    }
    $SetupArgs += @("--browser", $name)
}
if ($Force) { $SetupArgs += "--force" }
if ($SetupArguments) { $SetupArgs += $SetupArguments }
$SetupArgs = @($SetupArgs | ForEach-Object { Split-GluedArgument $_ } |
                ForEach-Object { Get-SafeArgument $_ })

$DryRun = $SetupArgs -contains "--dry-run"
$NoQuestions = ($SetupArgs -contains "--yes") -or ($SetupArgs -contains "--upgrade")
if ($SetupArgs -contains "--help") {
    Get-Help $MyInvocation.MyCommand.Path -Detailed
    exit 0
}

# --- uv ------------------------------------------------------------------------------------
if (-not (Find-Uv)) {
    Write-Host "uv, the Python package manager this project uses, was not found."
    if ($DryRun) {
        Write-Host "Dry run: uv would have to be installed first ($UvInstallPage). Nothing was changed."
        exit 0
    }
    if (-not (Test-CanAsk)) {
        Stop-Install ("Install uv first ($UvInstallPage), open a new PowerShell window and run " +
                      "this script again.")
    }
    Write-Host "It can be installed now for the current user (no administrator rights) with its"
    Write-Host "official installer: irm https://astral.sh/uv/install.ps1 | iex"
    $answer = Read-Host "Install uv now? [y/N]"
    if (@("y", "yes") -notcontains $answer.Trim().ToLowerInvariant()) {
        Stop-Install ("uv is needed. Install it ($UvInstallPage), open a new PowerShell window " +
                      "and run this script again.")
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "The uv installer failed (exit code $LASTEXITCODE). Install uv by hand: $UvInstallPage"
    }
    if (-not (Find-Uv)) {
        Stop-Install ("uv was installed but is not found from this window. Open a new PowerShell " +
                      "window and run this script again.")
    }
}

# Keep Python and the uv cache inside the project folder.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root ".python"
$env:UV_CACHE_DIR = Join-Path $Root ".uv-cache"

$Cli = Join-Path $Root ".venv\Scripts\compound-registry.exe"

Push-Location $Root
try {
    if ($DryRun) {
        if (-not (Test-Path $Cli)) {
            Write-Host "Dry run: the Python environment does not exist yet. An installation would"
            Write-Host "create it in $(Join-Path $Root '.venv') (uv sync --locked --no-dev) and then start"
            Write-Host "'compound-registry setup'. Nothing was changed."
            exit 0
        }
        Write-Host "==> Dry run: the Python environment is left as it is (no uv sync)"
    }
    else {
        while (@(Get-RunningHost).Count -gt 0) {
            Write-Host ""
            Write-Host "The registry program of this folder is running: a browser has it open, and its"
            Write-Host "file cannot be replaced meanwhile."
            # An update started by hand (--upgrade) waits here too; --yes never waits.
            if ((-not (Test-Console)) -or ($SetupArgs -contains "--yes")) {
                Write-Host "If the next step fails: $CloseHint"
                break
            }
            $answer = Read-Host "Close Firefox/Chrome and MestReNova, then press Enter (or type c to go on anyway)"
            if ($answer.Trim().ToLowerInvariant() -eq "c") { break }
        }
        Write-Host "==> Creating the Python environment (uv sync --locked --no-dev)"
        & uv sync --locked --no-dev
        if ($LASTEXITCODE -ne 0) {
            Stop-Install ("uv sync failed (exit code $LASTEXITCODE); see its message above. If it " +
                          "says that a file is in use or access is denied: $CloseHint")
        }
        if (-not (Test-Path $Cli)) {
            Stop-Install "$Cli was not created by uv sync."
        }
    }

    Write-Host ""
    & $Cli setup @SetupArgs
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($code -ne 0) {
    Write-Host ""
    Write-Host "compound-registry setup ended with exit code $code; see its messages above."
}
exit $code
