param(
    [string]$Target = 'C:\Users\Pure\Desktop\python\scrape_page_v2',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$TargetPath = [System.IO.Path]::GetFullPath($Target)

if (Test-Path -LiteralPath $TargetPath) {
    $Existing = @(Get-ChildItem -LiteralPath $TargetPath -Force)
    if ($Existing.Count -gt 0 -and -not $Force) {
        throw "โฟลเดอร์ปลายทางไม่ว่าง: $TargetPath (ใช้ -Force หากต้องการเขียนทับไฟล์ชื่อเดียวกัน)"
    }
} else {
    New-Item -ItemType Directory -Path $TargetPath -Force | Out-Null
}

Get-ChildItem -LiteralPath $Source -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $TargetPath $_.Name) -Force
}

$TargetTests = Join-Path $TargetPath 'tests'
New-Item -ItemType Directory -Path $TargetTests -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $Source 'tests') -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $TargetTests $_.Name) -Force
}

Write-Host "คัดลอก scrape_page_v2 ไปที่ $TargetPath สำเร็จ" -ForegroundColor Green
Write-Host "ขั้นต่อไป: cd '$TargetPath'; .\setup.ps1"
