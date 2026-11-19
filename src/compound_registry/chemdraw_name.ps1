# Asks the ChemDraw desktop program installed on this PC for the name of one
# structure. Called by compound_registry/chemdraw.py; not meant to be run by hand.
#
# Input  (stdin): one line, the molfile as base64 of its UTF-8 text.
# Output (stdout), ASCII lines only:
#   before:<ids>  the ChemDraw processes that were already running, written
#                 before ChemDraw is started (comma-separated, may be empty).
#                 If the caller has to stop this script, it uses the list to
#                 find the hidden ChemDraw that was started here (see
#                 chemdraw_cleanup.ps1);
#   result:<b64>  base64 of UTF-8 JSON, either
#                 {"ok":true,"name":...,"inchi":...,"atoms":n} or
#                 {"ok":false,"stage":"input|start|name","message":...}.
#
# Every start through this interface is a new, hidden ChemDraw process: a
# ChemDraw window the user has open is a different process and is not touched.
# Nothing is saved and nothing is sent anywhere.
param([string]$ProgId = 'ChemDraw_x64.Application')

$ErrorActionPreference = 'Stop'

function Send-Line([string]$text) {
    [Console]::Out.WriteLine($text)
    [Console]::Out.Flush()
}

function Send-Result($value) {
    $json = $value | ConvertTo-Json -Compress
    Send-Line ('result:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
}

function Get-ChemDrawIds {
    @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -eq 'ChemDraw' } |
        ForEach-Object { $_.Id })
}

$app = $null
$stage = 'input'
try {
    $line = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($line)) { throw 'No structure was given.' }
    $molfile = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line.Trim()))

    $stage = 'start'
    Send-Line ('before:' + ((Get-ChemDrawIds) -join ','))
    $app = New-Object -ComObject $ProgId

    $stage = 'name'
    $doc = $app.Documents.Add()
    $doc.Objects.Data('chemical/x-mdl-molfile') = $molfile
    $atoms = [int]$doc.Atoms.Count
    $name = [string]$doc.Objects.Data('chemical/x-name')
    $inchi = [string]$doc.Objects.Data('chemical/x-inchi')
    Send-Result @{ ok = $true; name = $name; inchi = $inchi; atoms = $atoms }
}
catch {
    Send-Result @{ ok = $false; stage = $stage; message = [string]$_.Exception.Message }
}
finally {
    if ($null -ne $app) {
        try { $app.Quit() } catch { }
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) } catch { }
    }
}
exit 0
