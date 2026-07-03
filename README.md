# print-intake — AI 인쇄 접수 시스템 프로토타입

파일 접수 → 검판(프리플라이트) → 견적 → 확정을 대화형 AI로 자동화하는 완동 프로토타입.
설계 원칙과 마일스톤은 [docs/PLAN.md](docs/PLAN.md), 아키텍처 결정은 [docs/decisions/](docs/decisions/) 참조.

## 실행 (윈도우 로컬)

```powershell
.venv\Scripts\python -m synth.generate_clean     # 정상 샘플 PDF 생성
.venv\Scripts\python -m synth.inject_defects     # 결함 주입 + manifest
.venv\Scripts\python -m pytest                   # 유닛테스트
.venv\Scripts\python -m evals.run_preflight_eval # 검출 재현율/오탐률 리포트
.venv\Scripts\python -m uvicorn api.main:app --port 8000   # 데모 서버
```

리눅스/도커에서는 `make gen-samples / test / eval / demo`.

## 환경변수

| 변수 | 용도 |
|---|---|
| `ANTHROPIC_API_KEY` | LLM 계층. 없으면 결정론적 목 어댑터로 동작 (데모 대화 품질 제한) |
| `ACCESS_CODE` | 데모 페이지 접속 코드. 설정 시 코드 입력해야 사용 가능 |
