# 로컬 실행 패키지 생성 — 받는 쪽은 압축 풀고 "시작하기.bat" 더블클릭이 전부.
# 파이썬 내장 배포판(embeddable) + 의존성 + 빌드된 UI + 샘플 PDF를 하나로 묶는다.
#
# 실행: powershell -ExecutionPolicy Bypass -File scripts\make_portable.ps1
# 산출: build\print-intake-포터블\  +  build\print-intake-portable.zip
#
# 전제: 이 저장소에서 개발 venv(.venv, Python 3.12)와 demo-ui\dist 빌드가 준비된 상태.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot          # print-intake 루트
$PyVersion = "3.12.10"
$EmbedUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"

$Build = Join-Path $Root "build"
$Dest = Join-Path $Build "print-intake-포터블"
$AppDir = Join-Path $Dest "app"
$PyDir = Join-Path $Dest "python"
$SiteDir = Join-Path $Dest "site-packages"

Write-Host "== 1/6 준비: $Dest"
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force $AppDir, $PyDir, $SiteDir | Out-Null

Write-Host "== 2/6 파이썬 내장 배포판 다운로드 ($PyVersion)"
$zip = Join-Path $Build "python-embed.zip"
if (-not (Test-Path $zip)) {
    Invoke-WebRequest -Uri $EmbedUrl -OutFile $zip
}
Expand-Archive -Path $zip -DestinationPath $PyDir -Force
# ._pth에 앱·패키지 경로 추가 (embeddable은 이 파일이 sys.path의 전부)
$pth = Get-ChildItem $PyDir -Filter "python*._pth" | Select-Object -First 1
@(
    "python312.zip"
    "."
    "..\site-packages"
    "..\app"
) | Set-Content -Path $pth.FullName -Encoding ascii

Write-Host "== 3/6 의존성 설치 (--target, cp312 win_amd64 휠)"
& "$Root\.venv\Scripts\pip.exe" install --quiet --target $SiteDir `
    fastapi "uvicorn[standard]" pikepdf pypdfium2 reportlab Pillow numpy `
    "pydantic>=2" "sqlalchemy>=2" pyyaml anthropic httpx python-multipart

Write-Host "== 4/6 앱 복사 (코드 + UI + 샘플)"
foreach ($d in @("core", "synth", "evals", "api", "docs")) {
    Copy-Item -Recurse -Force (Join-Path $Root $d) (Join-Path $AppDir $d)
}
New-Item -ItemType Directory -Force (Join-Path $AppDir "demo-ui") | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "demo-ui\dist") (Join-Path $AppDir "demo-ui\dist")
New-Item -ItemType Directory -Force (Join-Path $AppDir "data") | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "data\samples") (Join-Path $AppDir "data\samples")
# __pycache__ 제거 (용량·잡음)
Get-ChildItem $AppDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

Write-Host "== 5/6 실행 스크립트·안내문"
@'
@echo off
chcp 65001 >nul
cd /d "%~dp0app"
rem API 키가 있으면 대화 품질이 올라갑니다 (선택). api_key.txt 첫 줄에 키를 넣으세요.
if exist "%~dp0api_key.txt" set /p ANTHROPIC_API_KEY=<"%~dp0api_key.txt"
echo.
echo  AI 인쇄 접수 데모를 시작합니다. 이 창을 닫으면 데모가 종료됩니다.
echo  잠시 후 브라우저가 자동으로 열립니다... (주소: http://localhost:8712)
echo.
start "" cmd /c "ping -n 4 127.0.0.1 >nul && start http://localhost:8712"
"%~dp0python\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port 8712
'@ | Set-Content -Path (Join-Path $Dest "시작하기.bat") -Encoding utf8

@'
AI 인쇄 접수 시스템 — 프로토타입 (로컬 실행판)

실행 방법
1. "시작하기.bat"를 더블클릭합니다.
2. 잠시 후 브라우저가 열립니다. 안 열리면 직접 http://localhost:8712 접속.
3. 데모를 끝내려면 검은 창을 닫으면 됩니다.

체험용 파일은 app\data\samples\ 안에 있습니다.
- clean\ : 정상 인쇄 파일 5종 (바로 견적까지 진행됨)
- corpus\ : 일부러 문제를 넣은 파일 50종 (검판이 문제를 잡아내는 걸 볼 수 있음)
직접 만든 PDF를 올려도 됩니다.

설치가 필요 없고, 인터넷 연결 없이도 동작합니다.
올린 파일은 이 PC 밖으로 나가지 않습니다.

(선택) AI 대화 품질을 높이려면: api_key.txt 파일을 이 폴더에 만들고
첫 줄에 Anthropic API 키를 넣은 뒤 다시 시작하세요. 키가 없어도
접수-검판-견적-확정 전 과정이 그대로 동작합니다.
'@ | Set-Content -Path (Join-Path $Dest "읽어주세요.txt") -Encoding utf8

Write-Host "== 6/6 압축"
$zipOut = Join-Path $Build "print-intake-portable.zip"
if (Test-Path $zipOut) { Remove-Item $zipOut }
Compress-Archive -Path $Dest -DestinationPath $zipOut
$size = [math]::Round((Get-Item $zipOut).Length / 1MB, 1)
Write-Host "완료: $zipOut ($size MB)"
