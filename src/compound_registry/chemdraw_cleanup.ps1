# Stops a hidden ChemDraw that chemdraw_name.ps1 started but could not close
# (the script had to be stopped after a time-out). Called by
# compound_registry/chemdraw.py; not meant to be run by hand.
#
# A ChemDraw process is stopped only when ALL of this holds:
#   * it was not running before the naming call (-Before: the ids that were);
#   * Windows started it for automation (its command line has "-Embedding"),
#     which a ChemDraw opened by the user never has;
#   * it has no window.
# So a ChemDraw the user works in is never touched.
param([string]$Before = '')

$known = @($Before -split ',' | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ })
try {
    Get-CimInstance Win32_Process -Filter "Name='ChemDraw.exe'" -ErrorAction Stop |
        Where-Object { $known -notcontains [int]$_.ProcessId -and $_.CommandLine -match '(?i)[-/]Embedding' } |
        ForEach-Object {
            $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
            if ($null -ne $p -and $p.MainWindowHandle -eq 0) {
                Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                [Console]::Out.WriteLine("stopped:$($p.Id)")
            }
        }
}
catch { }
exit 0
