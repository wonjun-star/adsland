# 서버 실행 — anthropic_key.txt에 키가 있으면 LLM 모드로, 없으면 규칙 모드로 뜬다.
# 사용: powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 포트 8000에 떠 있는 기존 서버 정리
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

# API 키 로드 (주석·빈 줄 제외한 첫 줄)
$keyFile = Join-Path $root "anthropic_key.txt"
if (Test-Path $keyFile) {
    $key = (Get-Content $keyFile | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith("#") } | Select-Object -First 1)
    if ($key) {
        $env:ANTHROPIC_API_KEY = $key.Trim()
        Write-Host "LLM 모드: 키 로드됨 (LLM이 분류·파싱·대화 처리)"
    } else {
        Write-Host "규칙 모드: anthropic_key.txt에 키가 아직 없음"
    }
} else {
    Write-Host "규칙 모드: anthropic_key.txt 없음"
}

if (Test-Path (Join-Path $root "data\sessions.db")) { Remove-Item (Join-Path $root "data\sessions.db") -Force -ErrorAction SilentlyContinue }
& "$root\.venv\Scripts\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port 8000
