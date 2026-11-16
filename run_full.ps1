param(
    [switch]$Headed,
    [switch]$NoSubmit
)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "ยังไม่พบ .venv กรุณารัน .\setup.ps1 ก่อน"
}

$Arguments = @('main.py', '--full-run')
if ($Headed) {
    $Arguments += '--headed'
}
if ($NoSubmit) {
    $Arguments += '--no-submit'
}

Set-Location -LiteralPath $ProjectDir
& $Python @Arguments
exit $LASTEXITCODE
