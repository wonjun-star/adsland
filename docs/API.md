# API 계약 (데모 서버 ↔ 접수 UI)

FastAPI(`api/main.py`)와 React UI(`demo-ui/`)는 이 문서를 기준으로 맞춘다.

## 접속 제어

- 환경변수 `ACCESS_CODE`가 설정된 경우: `POST /api/access {"code": "..."}` 성공 시 쿠키 발급.
  이후 모든 `/api/*` 요청은 쿠키 필요, 없으면 401. `ACCESS_CODE` 미설정이면 전부 개방(로컬 개발).
- 정적 UI(빌드된 demo-ui)는 서버가 `/`에서 서빙. 접속 코드 입력 화면은 UI가 처리.

## 공통 응답 형태 (대화형 엔드포인트)

```json
{
  "session": {
    "id": "…", "state": "SLOT_FILLING", "product": "sticker",
    "customer_type": "A",
    "slots": {"size": {"value": "90x90", "source": "inferred", "confirmed": false}},
    "escalated": false, "confirmed": false
  },
  "reply": {"text": "…어시스턴트 한국어 응답…", "quick_options": ["100매", "500매", "1000매"]},
  "cards": []
}
```

`cards[]` 종류 (UI가 채팅 말풍선 아래 카드로 렌더):
- `{"type": "preflight_report", "results": [CheckResult…], "gate_ok": bool}` — 검판 결과표
- `{"type": "quote", "total": 24000, "currency": "KRW", "lines": […], "vat_included": true}`
- `{"type": "autofix_preview", "check_id": "bleed", "before_url": "/api/files/…png", "after_url": "…"}`
- `{"type": "file_preview", "url": "/api/files/…png"}` — 업로드 파일 1페이지 미리보기
- `{"type": "escalation", "reasons": […]}` — "사람 검판 큐로 이동했습니다"
- `{"type": "order_confirmed", "order_no": "…", "summary": {…}}` — 결제 목업 완료

## 엔드포인트

| 메서드/경로 | 요청 | 응답 |
|---|---|---|
| `POST /api/access` | `{code}` | 200 쿠키 / 401 |
| `POST /api/session` | – | 공통 응답 (인사말 포함) |
| `GET /api/session/{id}` | – | `session` + 지금까지의 `transcript` |
| `POST /api/session/{id}/message` | `{text}` | 공통 응답 |
| `POST /api/session/{id}/upload` | multipart `file` (PDF) | 공통 응답 (preflight_report·file_preview 카드) |
| `POST /api/session/{id}/autofix` | `{check_id}` | 공통 응답 (autofix_preview + 재검판 리포트) |
| `POST /api/session/{id}/confirm` | – | 공통 응답 (관문 통과 시 order_confirmed, 실패 시 blockers 설명) |
| `GET /api/files/{name}` | – | PNG/PDF 파일 |
| `GET /api/demo/board` | – | 왼쪽 패널용 합성 게시판 스레드 (타임스탬프 연출 포함) |
| `GET /api/health` | – | `{ok: true}` |

## 원칙

- 서버는 상태를 바꾸는 유일한 곳이며, 모든 응답에 최신 `session` 스냅샷을 담는다 (UI는 로컬 상태 추측 금지).
- `reply.text`는 LLM(또는 규칙 기반 폴백)이 만들지만, `cards`의 숫자·상태는 전부 결정론적 엔진 출력이다.
