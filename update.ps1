<#
.SYNOPSIS
    Brings an installed ChemLinker up to date (current user, no administrator rights).

.DESCRIPTION
    Double-click update.cmd, or run in a PowerShell window:

        powershell -ExecutionPolicy Bypass -File update.ps1

    The script
      1. fetches the new program files when this folder is a git checkout
         (git pull --ff-only). It stops when files in this folder were changed locally or
         the branch has diverged. When the folder is not a git checkout: download the new
         version, unpack it over this folder, then run this script;
      2. runs the installer with --upgrade: nothing is asked and every choice of the
         installation is kept (data folder, browsers, ELN address, MestReNova add-on). The
         Python environment and the browser registration are refreshed, the extension is
         built again, the add-on is refreshed if it is installed, and the stored structures
         are checked if the chemistry version changed;
      3. ends with the list of what is left to do by hand (the extension in the browser,
         MestReNova).

    The registry itself (database, journal, backups, compound files) is never replaced.

    Options of "compound-registry setup" can be added and are passed on, for example
    --dry-run (only print what would be done; with it nothing is fetched either).

.PARAMETER NoPull
    Do not fetch anything: use the program files that are in this folder. Same as --no-pull.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$NoPull,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SetupArguments = @()
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Stop-Update([string]$Message) {
    Write-Host ""
    Write-Host "ERROR: $Message"
    exit 1
}

# Started with "powershell -File" (update.cmd does that), a quoted folder that ends in a
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

function Get-ProgramVersion {
    $file = Join-Path $Root "src\compound_registry\__init__.py"
    if (Test-Path $file) {
        foreach ($line in Get-Content $file) {
            if ($line -match '^__version__\s*=\s*"([^"]+)"') { return $Matches[1] }
        }
    }
    return "unknown"
}

$Rest = @()
foreach ($argument in @($SetupArguments | ForEach-Object { Split-GluedArgument $_ })) {
    if ($argument -eq "--no-pull") { $NoPull = $true } else { $Rest += $argument }
}
$DryRun = $Rest -contains "--dry-run"

$Before = Get-ProgramVersion
Write-Host "ChemLinker: installed program version $Before"

if ($NoPull) {
    Write-Host "==> Not fetching anything (-NoPull): using the files in $Root"
}
elseif ($DryRun) {
    Write-Host "==> Dry run: nothing is fetched"
}
elseif (-not (Test-Path (Join-Path $Root ".git"))) {
    Write-Host "This folder is not a git checkout, so the new version is not fetched automatically."
    Write-Host "To get it: download the new version of the project and unpack it over this folder"
    Write-Host "($Root), replacing the files, then run this script again."
    Write-Host "==> Going on with the files that are here now"
}
elseif (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Stop-Update ("This folder is a git checkout, but git was not found. Install git, or download " +
                 "the new version, unpack it over this folder and run this script with -NoPull.")
}
else {
    $remotes = @(& git -C $Root remote)
    if ($LASTEXITCODE -ne 0) {
        Stop-Update "git could not read this folder ($Root); see its message above."
    }
    if ($remotes.Count -eq 0) {
        Write-Host "==> This git checkout has no remote to fetch from: using the files that are here"
    }
    else {
        $changes = @(& git -C $Root status --porcelain --untracked-files=no)
        if ($LASTEXITCODE -ne 0) {
            Stop-Update "git status failed in $Root; see its message above."
        }
        if ($changes.Count -gt 0) {
            Write-Host ""
            Write-Host "Program files in this folder were changed locally:"
            foreach ($change in $changes) { Write-Host "  $change" }
            Stop-Update ("Nothing was fetched or changed. Undo or commit these changes " +
                         "(to set them aside: git stash), then run this script again; or run it " +
                         "with -NoPull to update with the files as they are.")
        }
        $branch = [string](& git -C $Root symbolic-ref -q --short HEAD)
        if ($LASTEXITCODE -ne 0 -or $branch -eq "") {
            Stop-Update ("This checkout is at a fixed version (a tag or a single commit), not on " +
                         "a branch, so there is nothing to pull. Nothing was fetched or changed. " +
                         "To follow the new versions: git checkout main, then run this script " +
                         "again. To go to one version: git fetch --tags, git checkout <tag>, " +
                         "then run this script with -NoPull.")
        }
        $upstream = [string](& git -C $Root for-each-ref "--format=%(upstream)" "refs/heads/$branch")
        if ($upstream -eq "") {
            Stop-Update ("The branch '$branch' of this checkout has no upstream branch, so there " +
                         "is nothing to pull. Nothing was fetched or changed. Switch to the main " +
                         "branch (git checkout main) and run this script again; or get the new " +
                         "files with git yourself, then run this script with -NoPull.")
        }
        Write-Host "==> Fetching the new version (git pull --ff-only)"
        & git -C $Root pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            Stop-Update ("git pull --ff-only failed; see its message above. Nothing was updated. " +
                         "Usual reasons: no connection, or this checkout has commits of its own " +
                         "(a diverged branch), which git does not overwrite. Resolve that with " +
                         "git, or run this script with -NoPull to go on with the files that are " +
                         "here.")
        }
        $After = Get-ProgramVersion
        if ($After -eq $Before) {
            Write-Host "Program version: $After (unchanged)"
        }
        else {
            Write-Host "Program version: $Before -> $After"
        }
    }
}

Write-Host ""
& (Join-Path $Root "install.ps1") --upgrade @Rest
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "The update did not finish (exit code $code); see the messages above."
}
exit $code
