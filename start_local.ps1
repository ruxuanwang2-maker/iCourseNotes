$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
& "$PSScriptRoot\.venv\Scripts\python.exe" -u "$PSScriptRoot\run_local.py" @args
exit $LASTEXITCODE
