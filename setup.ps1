$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir
python -m venv --system-site-packages .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m playwright install chromium
Write-Host 'กำลังสร้าง/ตรวจสอบ Chrome profile สำหรับ Facebook' -ForegroundColor Cyan
& .\.venv\Scripts\python.exe createprofilechrome.py
if ($LASTEXITCODE -ne 0) {
    throw "สร้าง Chrome profile ไม่สำเร็จ (exit code $LASTEXITCODE)"
}
Write-Host 'ติดตั้ง scrape_page_v2 สำเร็จ' -ForegroundColor Green
