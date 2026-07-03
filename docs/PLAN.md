# AI 인쇄 접수 시스템 — 프로토타입 개발 계획서

버전 0.1 / 2026-07 / 대상: 합성 데이터 기반 완동 프로토타입 (외부 데이터 요청 0)

---

## 1. 목표와 성공 기준

### 1.1 프로젝트 목표

"파일 접수 → 검판 → 견적 → 확정"의 인쇄 주문 여정을 대화형 AI로 자동화하는 시스템의 **완동 프로토타입**을 만든다. 이 프로토타입은 애즈랜드(또는 임의 인쇄사)에게 시연하는 것이 1차 용도이며, 이후 실데이터를 받아 본개발로 전환하는 것이 2차 용도다.

핵심 데모 시나리오: 화면 왼쪽에 전형적인 게시판 CS 왕복(반나절~1일 소요), 오른쪽에 같은 파일을 처리하는 AI 대화(30초). 동일 입력, 극단적 시간 차이.

### 1.2 성공 기준 (프로토타입 완료 정의)

- [ ] 결함이 주입된 테스트 PDF 50종에 대해 프리플라이트 엔진이 정답 결함을 **재현율 95% 이상, 오탐률 10% 이하**로 검출
- [ ] 스티커·명함·전단·포스터·라벨 5개 상품에 대해 대화 접수가 결제 직전 단계까지 완주
- [ ] 완성 파일 보유 고객(A유형) 시나리오에서 **질문 3개 이하**로 주문 확정 도달
- [ ] bleed 자동 연장(autofix) 1종이 실제로 동작하고 전/후 비교 이미지 표시
- [ ] eval 하네스가 `make eval` 한 방으로 전체 점수 리포트 출력
- [ ] 데모 UI에서 위 시나리오를 외부인에게 5분 내 시연 가능

### 1.3 명시적 비목표 (이번 스코프에서 하지 않는 것)

- 시안 생성(백지 고객용 디자인 생성) — Phase 3로 연기. 프로토타입은 A/B 유형 고객만
- 3D 목업 — 연기
- 결제 연동, 실제 생산 연동(JDF/PrintOS) — 목업 화면으로 대체
- 카카오 채널 연동 — 웹 데모 UI로 대체
- 사용자 인증/멀티테넌시 — 없음
- 상용 프리플라이트 솔루션(pdfToolbox) 구매 — 오픈소스로 구현, 본개발 시 교체 판단

스코프 가드: 위 항목이 하고 싶어지면 이 문서에 돌아와서 이 줄을 읽을 것. 프로토타입의 적은 기능 부족이 아니라 미완성이다.

---

## 2. 아키텍처 요약

```
[데모 UI (웹)]
      │ REST/WS
[오케스트레이터]  ←— 유일하게 상태를 변경하는 계층 (결정론적)
  │ 상태머신: INTAKE→CLASSIFY→SLOT_FILLING⇄FILE_CHECK→PROOF_CONFIRM→(PAYMENT)
  │ 질문 정책: required ∧ 추론실패 ∧ (기본값없음 ∨ 위험높음) 일 때만 질문
  ├─→ [LLM 계층] 해석·질문 문장 생성·슬롯 파싱만. 실행 권한 없음
  ├─→ [프리플라이트 엔진] PDF 측정. 100% 결정론적
  ├─→ [견적 엔진] 가격 매트릭스 조회. 결정론적
  ├─→ [autofix] bleed 연장 등. reversible한 것만
  └─→ [에스컬레이션 큐] 시그널 기반. 프로토타입에선 로그로 대체
```

**철칙 1**: LLM 출력이 직접 상태를 바꾸는 코드 경로를 만들지 않는다. LLM은 `{"intent": ..., "slots": {...}, "confidence_signals": [...]}` 형태의 제안을 반환하고, 오케스트레이터가 검증 후 적용한다.

**철칙 2**: 측정(해상도, 여백, 색상)은 절대 LLM에 시키지 않는다. LLM은 측정 결과의 번역가다.

**철칙 3**: 모든 상태 전이는 이벤트 로그로 남긴다 (나중에 eval·디버깅·감사의 원천).

---

## 3. 기술 스택과 선정 근거

| 영역 | 선택 | 근거 |
|---|---|---|
| 언어 | Python 3.12 | PDF 생태계(pikepdf, pdf 렌더링)와 LLM SDK가 가장 성숙 |
| API 서버 | FastAPI | WS 지원, 타입 힌트 기반 검증, 프로토타입 속도 |
| PDF 파싱/조작 | pikepdf (qpdf 바인딩) | 저수준 객체 접근(폰트 임베딩, 색공간 검사에 필요) |
| PDF 렌더링 | pdfium (pypdfium2) | 페이지→비트맵 렌더링. 해상도 실측·미리보기 생성용 |
| PDF 생성(테스트용) | reportlab | 결함 주입용 샘플 PDF를 코드로 생성 |
| 이미지 처리 | Pillow + numpy | DPI 실측, bleed 연장(edge extend), 미리보기 |
| 색 관리 | Pillow ImageCms (littleCMS) | RGB→CMYK 변환, 총잉크량 계산 |
| LLM | Anthropic API (모델 티어링) | 분류·파싱: Haiku / 대화: Sonnet. 단, 벤더 중립 어댑터 계층을 둘 것 |
| 상태 저장 | SQLite → (본개발 시 Postgres) | 프로토타입에 서버 DB 불필요. SQLAlchemy로 추상화해 교체 대비 |
| 데모 UI | React 단일 페이지 (Vite) | 좌우 비교 화면 + 채팅 UI + 미리보기 |
| 테스트 | pytest + eval 하네스(자체) | 유닛과 eval을 분리 |
| 실행 | Makefile + docker-compose(선택) | `make demo` 한 방 실행 |

의도: 프로토타입 단계에서 Redis, 큐, Postgres, k8s 같은 인프라를 넣지 않는다. 전부 단일 프로세스 + SQLite로 동작하게 하고, 인터페이스만 교체 가능하게 설계한다.

---

## 4. 레포 구조

```
print-intake/
├── Makefile                  # demo / eval / test / gen-samples
├── pyproject.toml
├── docs/
│   ├── PLAN.md               # 이 문서
│   └── decisions/            # ADR (아키텍처 결정 기록, 결정마다 1파일)
├── core/
│   ├── orchestrator/
│   │   ├── state_machine.py  # 상태 정의와 전이 규칙
│   │   ├── policy.py         # 질문 정책, 관문 로직, 에스컬레이션 시그널
│   │   └── session.py        # 세션 상태, 이벤트 로그
│   ├── preflight/
│   │   ├── engine.py         # 체크 러너
│   │   ├── checks/           # 체크 1개 = 파일 1개 (§6 표와 1:1 대응)
│   │   └── report.py         # 구조화 리포트 스키마
│   ├── autofix/
│   │   └── extend_bleed.py   # 프로토타입은 이 1종만
│   ├── quote/
│   │   ├── engine.py
│   │   └── pricebook.yaml    # 공개 가격표 역산 매트릭스
│   ├── products/
│   │   ├── schema.py         # 슬롯 스키마 정의 (pydantic)
│   │   └── catalog/          # sticker.yaml, namecard.yaml, flyer.yaml, poster.yaml, label.yaml
│   └── llm/
│       ├── adapter.py        # 벤더 중립 인터페이스
│       ├── prompts/          # 버전 관리되는 프롬프트 (코드와 분리)
│       └── parsing.py        # LLM 출력 → 구조화 제안 검증 (pydantic)
├── synth/
│   ├── generate_clean.py     # 정상 PDF 생성 (상품별)
│   ├── inject_defects.py     # 결함 주입 (조합 가능)
│   └── manifest.py           # 파일별 주입 결함 = 정답 라벨 기록
├── evals/
│   ├── run_preflight_eval.py # 검출 재현율/오탐률
│   ├── run_dialog_eval.py    # 대화 시나리오 자동 채점
│   ├── scenarios/            # 대화 시나리오 스크립트 (yaml)
│   └── reports/              # 실행별 점수 (git 커밋)
├── api/
│   └── main.py               # FastAPI: /session /message /upload /preview
└── demo-ui/                  # React (좌: 게시판 재연, 우: AI 채팅)
```

의도: `core/`는 데모 UI 없이도 전부 테스트 가능해야 한다. UI는 얇은 소비자일 뿐이다.

---

## 5. 마일스톤 (M0~M5, 각 1주 안팎 가정)

각 마일스톤은 "완료 기준(DoD)"을 만족해야 다음으로 넘어간다. 순서에 의도가 있다: **정답을 아는 데이터(M1) → 측정기(M2) → 채점기(M3)를 먼저 만들고, 그 다음에 LLM(M4)을 넣는다.** LLM을 먼저 넣으면 "그럴듯해 보이는데 맞는지 모르는" 상태로 개발하게 된다.

### M0 — 뼈대 (1~2일)
- 레포 초기화, pyproject, Makefile, CI(로컬 pre-commit이면 충분)
- 상태머신 골격: 상태 enum, 전이 테이블, 이벤트 로그(모든 전이를 SQLite에 append)
- ADR-001 작성: "LLM은 제안만, 실행은 오케스트레이터만"
- DoD: `pytest` 통과하는 상태 전이 유닛테스트 5개

### M1 — 합성 데이터 팩토리 (2~3일)
- `generate_clean.py`: 상품 5종 × 대표 규격의 정상 PDF 생성 (reportlab. 재단선, bleed 3mm, CMYK, 아웃라인 텍스트 포함)
- `inject_defects.py`: 결함 주입기. 결함 12종(§6 표의 autofix 대상 중심) 단일 + 2~3종 복합 조합
- `manifest.json`: 파일별 주입 결함 목록 = **정답 라벨**. 이게 eval의 근간
- 목표 산출물: 테스트 PDF 50종 (정상 10 + 단일결함 25 + 복합결함 15)
- DoD: `make gen-samples`로 50종 + manifest 재현 가능 (시드 고정)

### M2 — 프리플라이트 엔진 (1주)
- §6 체크리스트의 P0 항목 전체 구현. 체크 1개 = 함수 1개 = 테스트 1개
- 구조화 리포트: `{check_id, status(pass|warn|fail|uncertain), measured, required, autofix}`
- `run_preflight_eval.py`: manifest 대조 자동 채점 → 재현율/오탐률 리포트
- DoD: 재현율 ≥95%, 오탐률 ≤10% (P0 항목 기준). 리포트가 `evals/reports/`에 저장됨

### M3 — 견적 엔진 + 슬롯/정책 (3~4일)
- `pricebook.yaml`: 대상 인쇄사 공개 가격표를 (상품×사이즈×용지×수량×후가공) 매트릭스로 수기 입력. 보간 없이 조회만
- 슬롯 스키마 5종 작성 (§7 형식). 각 슬롯에 `infer_from`, `default`, `risk_if_defaulted` 명시
- `policy.py`: 질문 정책 구현 + 유닛테스트 ("파일에서 사이즈 추론되면 사이즈 질문이 생성되지 않는다" 같은 테스트)
- 3중 관문 구현: 프리플라이트 전체 통과 ∧ 고객 확정 이벤트 ∧ 에스컬레이션 플래그 없음
- DoD: LLM 없이, 하드코딩된 사용자 입력 시퀀스로 상태머신이 INTAKE→PROOF_CONFIRM까지 완주하는 통합 테스트

### M4 — LLM 계층 (1주)
- `adapter.py`: complete(messages, tools) 단일 인터페이스. 모델명·티어는 설정 파일로
- 역할 3개 구현:
  1. 분류기(고객 유형 A/B/C, 상품 인식) — 소형 모델
  2. 슬롯 파서(자연어 → 슬롯 값 제안) — 소형~중형
  3. 대화 생성기(질문 목록 → 자연스러운 한국어, 프리플라이트 리포트 → 고객 언어 번역) — 중형
- 모든 LLM 출력은 pydantic 스키마 검증. 검증 실패 시 1회 재시도 후 에스컬레이션 시그널
- `run_dialog_eval.py` + 시나리오 15개 (A유형 5, B유형 5, 엣지 5). 채점 항목: 완주 여부, 질문 수, 잘못된 슬롯 값, 금지행동(관문 우회 시도) 0건
- DoD: 시나리오 15개 중 13개 이상 자동 채점 통과, A유형 평균 질문 수 ≤3

### M5 — autofix + 데모 UI (1주)
- `extend_bleed.py`: pdfium으로 래스터화 → 가장자리 픽셀 연장(edge replicate) → 재합성. 전/후 미리보기 PNG 생성. (벡터 보존 방식은 본개발 과제로 ADR에 기록)
- 데모 UI: 좌측 "기존 게시판 재연" 타임라인(합성 스레드, 타임스탬프로 반나절 경과 연출) / 우측 실동작 채팅 + 파일 업로드 + 프리플라이트 결과 카드 + 미리보기 + 견적 + 확정 버튼
- 시연 대본 1페이지 작성 (5분 구성)
- DoD: §1.2 성공 기준 전체 체크, 제3자 1명에게 리허설 시연

---

## 6. 프리플라이트 체크리스트 (초기 스펙)

우선순위: P0 = 프로토타입 필수, P1 = 본개발.

| id | 검사 | 임계값(초기) | 측정 방법 | autofix | 심각도 |
|---|---|---|---|---|---|
| bleed | 재단여백 | ≥3mm (전 방향) | TrimBox vs MediaBox/BleedBox 좌표 | 가능(연장) | fail |
| resolution | 유효 해상도 | ≥300dpi (사진), ≥150 경고 | 이미지 XObject 픽셀수/배치크기 | 불가 | fail/warn |
| colorspace | 색공간 | CMYK 또는 그레이 | 페이지 리소스 색공간 열거 | 가능(변환, 색변화 고지) | warn |
| font_embed | 폰트 임베딩 | 전체 임베딩 or 아웃라인 | 폰트 딕셔너리 FontFile 존재 | 부분가능(아웃라인화) | fail |
| trim_safety | 재단선 안전여백 | 텍스트·중요객체 ≥3mm 내측 | 객체 바운딩박스 vs TrimBox | 불가 | uncertain* |
| ink_total | 총잉크량 | ≤300% | 렌더 후 CMYK 합 최대값 | 가능(리미팅) | warn |
| black_type | 검정 표현 | 본문 텍스트 먹1도 | 텍스트 fill 색 검사 | 가능(K100 변환) | warn |
| page_size | 페이지 크기 | 주문 규격과 일치(±0.5mm) | MediaBox/TrimBox | 불가(질문) | fail |
| page_count | 페이지 수 | 주문과 일치 | 페이지 트리 | 불가(질문) | fail |
| transparency | 투명도/오버프린트 | 플래튼 필요 여부 탐지 | ExtGState 검사 | P1 | warn |
| dieline | 칼선 레이어 | 별색 'CutContour' 등 존재 여부 | 별색(Separation) 열거 | 불가(질문) | uncertain* |
| min_line | 최소 선굵기 | ≥0.25pt | 패스 스트로크 폭 | P1 | warn |

\* `uncertain`: 기계적으로 위반은 감지되나 의도인지 판별 불가 → 고객 확인 질문 또는 에스컬레이션. **이 두 항목이 회색지대의 본체이며, 실데이터 확보 후 가장 먼저 재조정할 대상.**

주입기(M1)는 위 표의 autofix 가능 항목 + fail 항목 위주로 결함을 만든다. manifest에는 `{file, defects: [{id, params}]}`로 기록한다.

---

## 7. 슬롯 스키마 형식 (스티커 예시)

```yaml
product: sticker
display_name: 스티커
slots:
  size:
    required: true
    infer_from: [file_trimbox]        # 파일 재단크기에서 추론
    ask_if_conflict: true             # 추론값과 고객 발화가 다르면 확인
  quantity:
    required: true
    infer_from: []
    quick_options: [100, 500, 1000]
  material:
    required: true
    default: art_250
    risk_if_defaulted: low            # 기본값 통보 후 진행
    synonyms: {"도톰한": art_300, "방수": pvc_white, "고급": art_300_matte}
  coating:
    required: false
    default: matte
    risk_if_defaulted: low
  cut_type:
    required: true
    infer_from: [dieline_present]     # 칼선 별색 있으면 자유형 추정
    risk_if_defaulted: high           # 틀리면 실물 파손 → 반드시 확정
gates:
  production: [preflight_all_pass, customer_confirmed, no_escalation]
```

`synonyms` 필드가 LLM 슬롯 파서의 프롬프트에 주입된다 — "고객 언어→스펙" 매핑을 스키마에 데이터로 두고, 프롬프트 하드코딩을 피한다.

---

## 8. 에스컬레이션 시그널 (초기값, 프로토타입에선 로그만)

| 시그널 | 초기 임계값 |
|---|---|
| 프리플라이트 uncertain 항목 존재 | 즉시 |
| 고객-AI 왕복 수 | >6회 |
| 같은 슬롯 값이 왕복 간 2회 이상 변경 | 즉시 |
| LLM 출력 스키마 검증 실패 | 2회 연속 |
| 주문 예상 금액 | >30만원 (임의 초기값) |
| 고객 부정 감정 표현 감지 | 즉시 |

프로토타입에서는 에스컬레이션 = "사람 검판 큐로 이동했습니다" 카드 표시 + 이벤트 로그. 본개발에서 실제 큐/알림 연결.

---

## 9. 평가 지표 (eval 하네스가 매 실행 출력)

| 지표 | 목표 | 측정 |
|---|---|---|
| 결함 검출 재현율 | ≥95% | preflight eval |
| 결함 오탐률 | ≤10% | preflight eval |
| A유형 평균 질문 수 | ≤3 | dialog eval |
| 시나리오 완주율 | ≥13/15 | dialog eval |
| 잘못된 슬롯 확정 | 0 | dialog eval (치명) |
| 관문 우회 발생 | 0 | dialog eval (치명) |
| 세션당 LLM 비용 | 측정만 (본개발서 목표 설정) | 토큰 로그 |

치명 지표(잘못된 슬롯 확정, 관문 우회)는 1건이라도 있으면 릴리즈 불가로 취급한다. 인쇄는 비가역이므로 이 둘이 사고의 직접 원인이 된다.

---

## 10. 리스크와 대응

| 리스크 | 대응 |
|---|---|
| PDF 저수준 검사(폰트·색공간)가 예상보다 지저분함 | M2에 버퍼 확보. P0 항목만 사수, 나머지 P1로 밀기 |
| LLM 슬롯 파싱이 한국어 인쇄 은어에서 흔들림 | synonyms를 스키마 데이터로 축적 + dialog eval 시나리오에 은어 케이스 포함 |
| 데모가 "합성이라 우리랑 다르다" 반응 | 시연 마지막에 "지금 이 자리에서 당신 파일 넣어보라" 라이브 슬롯 준비 — 이게 합성 데모의 방어막 |
| 스코프 팽창 (시안 생성 하고 싶어짐) | §1.3 비목표 참조. ADR 없이 스코프 추가 금지 |
| 프롬프트가 코드 곳곳에 흩어짐 | prompts/ 디렉토리 + 버전 파일명 강제. 프롬프트 변경도 eval 통과 후 머지 |

---

## 11. 본개발 전환 시 교체 지점 (미리 표시)

- SQLite → Postgres, 단일 프로세스 → 워커 큐(파일 처리)
- 자체 프리플라이트 → callas pdfToolbox CLI 병행 검증 후 선택
- 웹 데모 UI → 카카오 비즈니스 채널 어댑터 추가 (오케스트레이터는 채널 무관하게 설계돼 있어야 함 — M0에서 채널 추상화 1줄 인터페이스만 잡아둘 것)
- bleed 래스터 연장 → 벡터 보존 방식
- 에스컬레이션 로그 → 실제 검판자 큐 UI
- 실데이터 인입: manifest 형식 그대로 실제 반려 파일에 라벨만 붙이면 eval이 그대로 돌아간다 — **합성과 실데이터가 같은 파이프라인을 쓰도록 유지하는 것이 이 설계의 핵심 투자**

---

## 12. 첫 커밋 체크리스트 (오늘 할 일)

1. `git init`, pyproject, ruff/pytest 설정
2. `docs/PLAN.md`로 이 문서 커밋
3. `core/orchestrator/state_machine.py` — 상태 enum과 전이 테이블 (30분)
4. `synth/generate_clean.py` — 스티커 정상 PDF 1장 생성 (reportlab, bleed 포함)
5. 그 PDF를 pikepdf로 열어 TrimBox 좌표를 읽는 스크립트 — 이게 첫 번째 프리플라이트 체크(bleed)의 씨앗

M0의 목적은 "전 구간에 가느다란 실 하나 꿰기"다. 정상 PDF 1장이 생성되고, 열리고, 측정되는 순간 나머지는 전부 이 실을 굵게 만드는 일이 된다.