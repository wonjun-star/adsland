# demo-ui — AI 파일접수·검판 데스크 (프로토타입 UI)

인쇄 주문 접수 여정(파일 업로드 → 검판 → 견적 → 확정)을 대화형으로 처리하는
React 단일 페이지. 서버 계약은 `docs/API.md`가 기준이다.

## 실행

```bash
npm run dev     # 개발 서버 (/api 는 localhost:8000 FastAPI 로 프록시)
npm run build   # dist/ 산출 — FastAPI 가 / 에서 정적 서빙
npm run lint    # oxlint
```

## 구조

- `src/api.js` — fetch 래퍼 (credentials: include, 401 → 접속 코드 게이트)
- `src/labels.js` — 기계 코드(체크 id·슬롯 값·상태) → 한국어 라벨 사전
- `src/App.jsx` — 세션 시작/턴 처리. 서버 응답의 `session` 스냅샷이 유일한 상태 원천
- `src/components/`
  - `AccessGate` — POST /api/access 접속 코드 입력
  - `ChatPane` — 말풍선·빠른 선택·확정 바·입력창·PDF 드래그&드롭
  - `Cards` — preflight_report / quote / autofix_preview / file_preview / escalation / order_confirmed
  - `SidePanel` — 주문 요약(슬롯 표 + 접수→검판→견적→확정 진행 표시)
  - `BoardPanel` — 비교 데모 모드의 좌측 게시판 재연 (GET /api/demo/board)

## 원칙

- 상태를 바꾸는 곳은 서버뿐 — UI 는 응답의 `session` 을 그대로 표기하고 로컬 추측을 하지 않는다.
- 카드의 숫자·판정은 전부 결정론적 엔진 출력이며 UI 는 라벨 변환만 담당한다.
- 외부 UI 라이브러리·CDN 없이 오프라인 빌드 가능해야 한다.
