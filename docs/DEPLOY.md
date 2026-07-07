# 배포 가이드 — 무료로 URL 하나 만들어 애즈랜드에 전달하기

목표 상태: `https://adsland.onrender.com` 같은 주소 + 접속 코드를 애즈랜드 담당자에게
보내면, 담당자가 브라우저에서 바로 파일을 올려 검판·견적·확정까지 체험할 수 있다.

## 1안 (기본): Render 무료 플랜

비용 0원. 단, 15분간 접속이 없으면 잠들었다가 첫 접속에 40~50초 걸린다 — 3단계의
핑 설정으로 해결한다.

### 준비물
- GitHub 계정 (저장소는 private여도 됨)
- Render 계정 (https://render.com — GitHub 로그인)
- Anthropic API 키 (https://console.anthropic.com — 없으면 규칙 기반 응답으로도 동작하지만
  대화 품질을 위해 권장. 데모 트래픽 기준 월 수천 원 수준)

### 순서

1. **GitHub에 올리기**
   ```powershell
   cd print-intake
   git remote add origin https://github.com/<계정>/adsland.git
   git push -u origin master
   ```

2. **Render에 연결**
   - Render 대시보드 → New → Blueprint → 저장소 선택.
   - `render.yaml`을 자동 인식한다. 환경변수 두 개를 입력:
     - `ANTHROPIC_API_KEY`: API 키 (없으면 비워둬도 동작)
     - `ACCESS_CODE`: 애즈랜드에 전달할 접속 코드 (예: `adland2026`)
   - Deploy — 첫 빌드는 10분 안팎 (도커 빌드 + 샘플 PDF 생성).

3. **잠들지 않게 핑 걸기**
   - https://uptimerobot.com 무료 가입 → New Monitor → HTTP(s) →
     URL에 `https://<배포주소>/api/health`, 간격 5분.
   - 이후로는 사실상 항상 깨어 있다.

4. **애즈랜드에 전달**
   - 주소 + 접속 코드 + `docs/DEMO_SCRIPT.md`(시연 대본) 한 장.

### 주의
- 무료 플랜은 디스크가 휘발성이다. 재배포·재시작 시 세션 기록이 사라진다 —
  프로토타입 시연에는 문제없고, 본개발에서 Postgres로 교체한다 (PLAN §11).
- API 키는 절대 저장소에 커밋하지 않는다. Render 대시보드에서만 입력.

## 2안: Hugging Face Spaces (Docker)

메모리가 더 필요하면(대형 포스터 PDF 반복 처리 등) HF Spaces 무료 티어(16GB)가 낫다.
Space 유형을 Docker로 만들고 이 저장소를 푸시하면 같은 Dockerfile로 뜬다.
포트는 `PORT` 환경변수 대신 7860을 쓰므로 Space 설정에서 `PORT=7860` 지정.

## 3안: 시연 자리에서만 임시 공개 (비용 0, 계정 불필요)

내 PC에서:
```powershell
.venv\Scripts\python -m uvicorn api.main:app --port 8000
# 별도 터미널에서
cloudflared tunnel --url http://localhost:8000
```
`cloudflared`가 발급하는 `https://….trycloudflare.com` 주소를 그 자리에서 공유.
PC를 끄면 주소도 사라진다.

## 로컬 확인 (배포 전 점검)

```powershell
cd print-intake
cd demo-ui; npm run build; cd ..          # UI 빌드 → demo-ui/dist
$env:ACCESS_CODE="test123"                 # 접속 코드 동작 확인용 (생략 가능)
.venv\Scripts\python -m uvicorn api.main:app --port 8000
# 브라우저: http://localhost:8000
```
