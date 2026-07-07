# adsland 배포용 — GitHub에 올리기 (push) 도우미
#
# 사전: github.com/new 에서 빈 저장소를 하나 만든다 (이름: adsland, README 없이 빈 것).
#   → 만들면 나오는 주소를 아래처럼 넣어 실행:
#
#   powershell -ExecutionPolicy Bypass -File scripts\publish.ps1 -RepoUrl https://github.com/<계정>/adsland.git
#
# 그다음 Render(https://render.com) → New → Blueprint → 이 저장소 선택 →
#   환경변수 ANTHROPIC_API_KEY, ACCESS_CODE 입력 → Deploy. (자세한 건 docs/DEPLOY.md)

param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "== 키 파일이 커밋에 안 들어갔는지 확인 ==" -ForegroundColor Cyan
$tracked = git ls-files | Select-String -Pattern "anthropic_key|\.env$|secret" -CaseSensitive:$false
if ($tracked) {
    Write-Host "중단: 민감 파일이 추적되고 있습니다:" -ForegroundColor Red
    $tracked
    exit 1
}
Write-Host "안전 — 추적되는 키/시크릿 파일 없음.`n" -ForegroundColor Green

Write-Host "== 원격(origin) 연결 ==" -ForegroundColor Cyan
$hasOrigin = git remote | Select-String -Pattern "^origin$"
if ($hasOrigin) {
    git remote set-url origin $RepoUrl
} else {
    git remote add origin $RepoUrl
}
git remote -v

Write-Host "`n== GitHub에 올리기 (push) ==" -ForegroundColor Cyan
Write-Host "처음이면 GitHub 로그인 창이 뜹니다 — 계정으로 인증하세요." -ForegroundColor Yellow
$branch = git branch --show-current
git push -u origin $branch

Write-Host "`n올리기 완료!" -ForegroundColor Green
Write-Host "이제 배포:" -ForegroundColor Cyan
Write-Host "  1) https://render.com 접속 (GitHub로 로그인)"
Write-Host "  2) New -> Blueprint -> 방금 올린 저장소 선택 (render.yaml 자동 인식)"
Write-Host "  3) 환경변수 입력:"
Write-Host "       ANTHROPIC_API_KEY = (사장님 Anthropic 키)"
Write-Host "       ACCESS_CODE       = (애즈랜드에 줄 접속 코드, 예: adland2026)"
Write-Host "  4) Deploy (~10분) -> 나온 주소가 https://adsland.onrender.com 형태"
Write-Host "  5) UptimeRobot로 <주소>/api/health 5분 핑 (잠들지 않게) — docs/DEPLOY.md 참고"
