# adsland — AI 인쇄 접수·검판 시스템 (애즈랜드 프로토타입)

파일 접수 → 검판(프리플라이트) → 견적 → 확정을 대화형 AI로 자동화하는 완동 프로토타입.
설계 원칙과 마일스톤은 [docs/PLAN.md](docs/PLAN.md), 아키텍처 결정은 [docs/decisions/](docs/decisions/),
배포·전달 방법은 [docs/DEPLOY.md](docs/DEPLOY.md), 시연 진행은 [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) 참조.

**API 키 없이 전 구간이 동작한다.** `ANTHROPIC_API_KEY`가 없으면 규칙 기반 응답으로
접수부터 주문 확정까지 완주하며, LLM 호출(과금)은 한 번도 일어나지 않는다.
키를 넣으면 대화 생성만 LLM으로 바뀌고, 측정·판정·가격은 언제나 결정론 엔진이다 (ADR-001).

## 실행 (윈도우 로컬)

```powershell
# 데모 서버 (키 없이 = 무료 규칙 기반)
.venv\Scripts\python -m uvicorn api.main:app --port 8000
# 브라우저: http://localhost:8000

# 내 API 키로 대화 품질 테스트할 때만
$env:ANTHROPIC_API_KEY = "sk-ant-..."
.venv\Scripts\python -m uvicorn api.main:app --port 8000
```

테스트·평가:

```powershell
.venv\Scripts\python -m pytest                   # 전체 유닛·통합 테스트
.venv\Scripts\python -m evals.run_preflight_eval # 결함 검출 채점 (재현율/오탐)
.venv\Scripts\python -m evals.run_dialog_eval    # 대화 시나리오 15종 채점
.venv\Scripts\python -m synth.generate_clean     # 샘플 재생성 (시드 고정)
.venv\Scripts\python -m synth.inject_defects
```

리눅스/도커에서는 `make gen-samples / test / eval / demo`.

## 전달 방법 2가지

| 방식 | 명령/절차 | 특징 |
|---|---|---|
| 클라우드 URL | [docs/DEPLOY.md](docs/DEPLOY.md) — Render 무료 + 접속 코드 | 항상 최신, 상대방 설치 없음 |
| 로컬 실행 압축본 | `powershell -ExecutionPolicy Bypass -File scripts\make_portable.ps1` → `build\print-intake-portable.zip` | 상대방은 압축 풀고 `시작하기.bat` 더블클릭. 오프라인 동작, 파일이 외부로 안 나감 |

## 현재 성적 (계획서 §1.2 성공 기준)

- 결함 주입 PDF 50종 검출: **재현율 100% / 오탐 0%** (목표 95%/10%)
- 대화 시나리오: **15/15 통과**, A유형 평균 질문 **0.4개** (목표 13/15, ≤3개)
- 치명 지표(잘못된 슬롯 확정·관문 우회): **0건**
- bleed 자동 보정: 전/후 비교 표시 + **칼선 별색 벡터 보존** + 보정 후 관문 통과
- 상품 5종(스티커·명함·전단·포스터·라벨) 결제 직전 단계까지 완주

## 환경변수

| 변수 | 용도 |
|---|---|
| `ANTHROPIC_API_KEY` | LLM 대화 생성 (선택). 없으면 규칙 기반 — 과금 0 |
| `ACCESS_CODE` | 데모 페이지 접속 코드. 설정 시 코드를 입력해야 사용 가능 |
| `PRINT_INTAKE_DB` | 세션 DB 경로/URL (기본 `data/sessions.db`) |
