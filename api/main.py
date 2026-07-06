"""FastAPI 데모 서버 — docs/API.md 계약의 구현체.

계층 역할 (ADR-001): 이 모듈은 HTTP ↔ ChatPipeline 변환만 한다.
상태 변경은 전부 IntakeService(오케스트레이터)가 하고, 여기서는
  - 파일 저장(업로드)과 미리보기 PNG 렌더(pdfium)
  - 카드의 로컬 경로 → /api/files/ URL 변환
  - 접속 코드 검사(서명 쿠키)
만 담당한다. 모든 응답에는 최신 session 스냅샷을 담는다 (UI는 로컬 추측 금지).

구동:  python -m uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.board_demo import get_board_thread
from core.orchestrator.chat import ChatPipeline
from core.orchestrator.service import PREVIEW_DIR, UPLOAD_DIR, IntakeService, TurnResult
from core.orchestrator.session import PROJECT_ROOT

DIST_DIR = PROJECT_ROOT / "demo-ui" / "dist"

#: 업로드 제한: PDF 매직 바이트 + 최대 30MB
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
PDF_MAGIC = b"%PDF"

#: 접속 쿠키 이름과 서명 문구 (값 = HMAC-SHA256(key=ACCESS_CODE, msg=문구))
ACCESS_COOKIE = "pi_access"
_ACCESS_SIGN_MSG = b"print-intake-access-v1"

#: 접속 코드 없이 통과하는 API 경로 (정적 파일은 /api/ 밖이라 자동 통과)
_OPEN_API_PATHS = {"/api/access", "/api/health"}

#: 파일명에서 허용하는 문자 (한글 포함). 나머지는 _ 로 치환
_SAFE_NAME_RE = re.compile(r"[^\w.\- 가-힣]", re.UNICODE)


# ---------------------------------------------------------------- 요청 본문 모델


class AccessBody(BaseModel):
    code: str


class MessageBody(BaseModel):
    text: str


class AutofixBody(BaseModel):
    check_id: str


class SelectBody(BaseModel):
    slot: str
    value: object


class ReopenBody(BaseModel):
    slot: str


class DesignBody(BaseModel):
    template: str | None = None
    fields: dict | None = None


# ---------------------------------------------------------------- 접속 제어


def _access_code() -> str | None:
    """환경변수 ACCESS_CODE. 매 요청 시점에 읽는다 (테스트에서 주입 가능)."""
    return os.environ.get("ACCESS_CODE") or None


def _sign(code: str) -> str:
    return hmac.new(code.encode("utf-8"), _ACCESS_SIGN_MSG, hashlib.sha256).hexdigest()


def _cookie_valid(request: Request) -> bool:
    code = _access_code()
    if code is None:
        return True  # ACCESS_CODE 미설정 → 전부 개방 (로컬 개발)
    got = request.cookies.get(ACCESS_COOKIE, "")
    return hmac.compare_digest(got, _sign(code))


# ---------------------------------------------------------------- 응답 조립


def _to_file_url(local_path: str | None) -> str | None:
    """previews 디렉터리 내부 파일만 /api/files/ URL로 변환. 밖이면 None (경로 탈출 차단)."""
    if not local_path:
        return None
    try:
        rel = Path(local_path).resolve().relative_to(PREVIEW_DIR.resolve())
    except ValueError:
        return None
    return f"/api/files/{rel.as_posix()}"


def _publish_cards(cards: list[dict]) -> list[dict]:
    """서비스가 만든 카드를 UI 계약 형태로: autofix_preview의 로컬 경로 → URL."""
    out: list[dict] = []
    for card in cards:
        if card.get("type") == "autofix_preview":
            before = _to_file_url(card.get("before"))
            after = _to_file_url(card.get("after"))
            if before and after:
                out.append(
                    {
                        "type": "autofix_preview",
                        "check_id": card.get("check_id"),
                        "before_url": before,
                        "after_url": after,
                    }
                )
            continue  # URL 변환 불가한 미리보기는 카드 자체를 내보내지 않는다
        if card.get("type") == "design_preview":
            published = {k: v for k, v in card.items() if k != "preview"}
            published["preview_url"] = _to_file_url(card.get("preview"))
            out.append(published)
            continue
        if card.get("type") == "change_summary":
            items = []
            for it in card.get("items", []):
                items.append(
                    {
                        **{k: v for k, v in it.items() if k not in ("before_preview", "after_preview")},
                        "before_url": _to_file_url(it.get("before_preview")),
                        "after_url": _to_file_url(it.get("after_preview")),
                    }
                )
            out.append(
                {
                    "type": "change_summary",
                    "product": card.get("product"),
                    "items": items,
                    "original_url": _to_file_url(card.get("original_preview")),
                    "final_url": _to_file_url(card.get("final_preview")),
                }
            )
            continue
        if card.get("type") == "order_confirmed":
            published = {k: v for k, v in card.items() if k not in ("final_preview", "back_preview")}
            published["final_url"] = _to_file_url(card.get("final_preview"))
            published["back_url"] = _to_file_url(card.get("back_preview"))
            out.append(published)
            continue
        if card.get("type") == "confirm_review":
            published = {k: v for k, v in card.items() if k not in ("preview", "back_preview")}
            published["preview_url"] = _to_file_url(card.get("preview"))
            published["back_url"] = _to_file_url(card.get("back_preview"))
            out.append(published)
            continue
        out.append(card)
    return out


def _turn_response(result: TurnResult, reply: str, extra_cards: list[dict] | None = None) -> dict:
    """공통 응답 형태 {session, reply, cards} (docs/API.md).

    reply.questions: 대기 중인 질문마다 선택지(options)를 담아 UI가 클릭 버튼으로 보여준다.
    reply.quick_options: 첫 질문 옵션 (구버전 호환).
    """
    questions = [
        {
            "slot": q.slot,
            "label": q.display_name or q.slot,
            "options": list(q.options or q.quick_options),
            "allow_other": q.allow_other,
        }
        for q in result.directives.questions
    ]
    quick_options = list(questions[0]["options"]) if questions else []
    cards = _publish_cards(result.cards) + list(extra_cards or [])
    return {
        "session": result.session.model_dump(mode="json"),
        "reply": {"text": reply, "quick_options": quick_options, "questions": questions},
        "cards": cards,
    }


def _render_first_page_preview(pdf_path: Path, session_id: str) -> str | None:
    """업로드 PDF 1페이지 → PNG 미리보기 (pdfium). 실패해도 업로드 흐름은 계속."""
    try:
        import pypdfium2 as pdfium

        out_dir = PREVIEW_DIR / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{pdf_path.stem}_p1.png"
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            page = doc[0]
            image = page.render(scale=1.5).to_pil()
            page.close()
        finally:
            doc.close()
        if image.width > 800:
            ratio = 800 / image.width
            image = image.resize((800, max(1, int(image.height * ratio))))
        image.save(out_path)
        return str(out_path)
    except Exception:
        return None  # 미리보기는 부가 기능 — 렌더 실패가 접수를 막으면 안 된다


def _safe_filename(original: str | None) -> str:
    name = _SAFE_NAME_RE.sub("_", Path(original or "").name).strip() or "upload.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


# ---------------------------------------------------------------- 앱 팩토리


def create_app() -> FastAPI:
    app = FastAPI(title="print-intake", docs_url=None, redoc_url=None)
    pipeline = ChatPipeline(IntakeService())

    # -------------------------------------------------------- 미들웨어
    # 등록 순서: 접속 검사 → CORS. (나중에 add된 쪽이 바깥이라 401에도 CORS 헤더가 붙는다)

    @app.middleware("http")
    async def access_guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in _OPEN_API_PATHS:
            if not _cookie_valid(request):
                return JSONResponse(status_code=401, content={"detail": "접속 코드가 필요합니다."})
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # vite dev
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -------------------------------------------------------- 공통 헬퍼

    def run_or_404(fn, *args):
        """세션 미존재(KeyError) → 404. 나머지 예외는 FastAPI 기본 처리."""
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.") from None

    # -------------------------------------------------------- 접속/헬스

    @app.post("/api/access")
    def access(body: AccessBody):
        code = _access_code()
        if code is None:
            return JSONResponse({"ok": True, "open": True})  # 개방 모드 — 쿠키 불필요
        if not hmac.compare_digest(body.code.encode("utf-8"), code.encode("utf-8")):
            raise HTTPException(status_code=401, detail="접속 코드가 올바르지 않습니다.")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            ACCESS_COOKIE,
            _sign(code),
            max_age=12 * 3600,
            httponly=True,
            samesite="lax",
        )
        return resp

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # -------------------------------------------------------- 세션/대화

    @app.post("/api/session")
    def create_session():
        result, reply = pipeline.start()
        return _turn_response(result, reply)

    @app.get("/api/session/{session_id}")
    def get_session(session_id: str):
        view = run_or_404(pipeline.service.view_session, session_id)
        transcript = pipeline.service.transcript(session_id)
        return {"session": view.model_dump(mode="json"), "transcript": transcript}

    @app.get("/api/session/{session_id}/ordersheet")
    def get_ordersheet(session_id: str):
        """오더지 — 내부 검수자·생산에게 전달되는 작업지시서."""
        sheet = run_or_404(pipeline.service.order_sheet, session_id)
        # 파일 미리보기 로컬 경로 → URL
        file_info = dict(sheet.get("file") or {})
        file_info["preview_url"] = _to_file_url(file_info.pop("preview", None))
        sheet["file"] = file_info
        for ch in sheet.get("changes", []):
            ch["before_url"] = _to_file_url(ch.pop("before_preview", None))
            ch["after_url"] = _to_file_url(ch.pop("after_preview", None))
        return sheet

    @app.post("/api/session/{session_id}/message")
    def post_message(session_id: str, body: MessageBody):
        result, reply = run_or_404(pipeline.process_message, session_id, body.text)
        return _turn_response(result, reply)

    @app.post("/api/session/{session_id}/upload")
    async def post_upload(session_id: str, file: UploadFile):
        run_or_404(pipeline.service.view_session, session_id)  # 세션 먼저 확인

        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="파일이 너무 큽니다 (최대 30MB).")
        if not content.startswith(PDF_MAGIC):
            raise HTTPException(status_code=400, detail="PDF 파일만 업로드할 수 있습니다.")

        dest_dir = UPLOAD_DIR / session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        # 매 업로드를 고유 경로에 저장 — 같은 파일명을 두 번 올려도 앞 파일을 덮지 않게
        safe = _safe_filename(file.filename)
        seq = len(list(dest_dir.glob("up_*")))
        dest = dest_dir / f"up_{seq:02d}_{safe}"
        dest.write_bytes(content)

        result, reply = run_or_404(pipeline.process_upload, session_id, dest, safe)

        extra: list[dict] = []
        preview = _render_first_page_preview(dest, session_id)
        url = _to_file_url(preview)
        if url:
            extra.append({"type": "file_preview", "url": url})
        return _turn_response(result, reply, extra_cards=extra)

    @app.post("/api/session/{session_id}/uploads")
    async def post_uploads(session_id: str, files: list[UploadFile]):
        """여러 파일을 한 번에 접수 (예: 명함 앞면·뒷면). 순서대로 병합 처리하고
        결과는 한 번만 보여준다 — 하나 보고 기다렸다 또 하나 보는 일이 없게."""
        run_or_404(pipeline.service.view_session, session_id)
        if not files:
            raise HTTPException(status_code=400, detail="파일이 없습니다.")

        dest_dir = UPLOAD_DIR / session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved: list[tuple[Path, str]] = []
        for file in files:
            content = await file.read()
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="파일이 너무 큽니다 (최대 30MB).")
            if not content.startswith(PDF_MAGIC):
                raise HTTPException(status_code=400, detail="PDF 파일만 업로드할 수 있습니다.")
            safe = _safe_filename(file.filename)
            seq = len(list(dest_dir.glob("up_*")))
            dest = dest_dir / f"up_{seq:02d}_{safe}"
            dest.write_bytes(content)
            saved.append((dest, safe))

        # 앞면 → (있으면) 뒷면 순으로 반영, 마지막 검판 결과만 반환
        result = reply = None
        for dest, safe in saved:
            result, reply = run_or_404(pipeline.process_upload, session_id, dest, safe)

        extra: list[dict] = []
        for dest, _safe in saved:
            url = _to_file_url(_render_first_page_preview(dest, session_id))
            if url:
                extra.append({"type": "file_preview", "url": url})
        return _turn_response(result, reply, extra_cards=extra)

    @app.post("/api/session/{session_id}/autofix")
    def post_autofix(session_id: str, body: AutofixBody):
        result, reply = run_or_404(pipeline.process_autofix, session_id, body.check_id)
        return _turn_response(result, reply)

    @app.post("/api/session/{session_id}/design")
    def post_design(session_id: str, body: DesignBody):
        """명함 시안 생성/재생성 (템플릿 변경·내용 수정)."""
        result, reply = run_or_404(
            pipeline.process_design, session_id, body.template, body.fields
        )
        return _turn_response(result, reply)

    @app.post("/api/session/{session_id}/select")
    def post_select(session_id: str, body: SelectBody):
        """질문 옵션 버튼 클릭 → 슬롯 직접 설정."""
        result, reply = run_or_404(pipeline.process_select, session_id, body.slot, body.value)
        return _turn_response(result, reply)

    @app.post("/api/session/{session_id}/reopen")
    def post_reopen(session_id: str, body: ReopenBody):
        """최종 확인 카드에서 특정 항목 '바꾸기' → 그 슬롯을 다시 고르게 띄운다."""
        result, reply = run_or_404(pipeline.process_reopen, session_id, body.slot)
        return _turn_response(result, reply)

    @app.post("/api/session/{session_id}/confirm")
    def post_confirm(session_id: str):
        result, reply = run_or_404(pipeline.process_confirm, session_id)
        return _turn_response(result, reply)

    # -------------------------------------------------------- 파일/데모/정적

    @app.get("/api/files/{name:path}")
    def get_file(name: str):
        """미리보기 파일 서빙. previews 디렉터리 내부만 허용 (경로 탈출 차단)."""
        base = PREVIEW_DIR.resolve()
        try:
            target = (base / name).resolve()
            target.relative_to(base)
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.") from None
        if not target.is_file() or target.suffix.lower() not in (".png", ".pdf"):
            raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
        media = "image/png" if target.suffix.lower() == ".png" else "application/pdf"
        return FileResponse(target, media_type=media)

    @app.get("/api/demo/board")
    def demo_board():
        return get_board_thread()

    if (DIST_DIR / "index.html").is_file():

        class SPAStaticFiles(StaticFiles):
            """빌드된 SPA 서빙: 없는 경로는 index.html로 폴백 (클라이언트 라우팅)."""

            async def get_response(self, path: str, scope):
                try:
                    return await super().get_response(path, scope)
                except StarletteHTTPException as e:
                    if e.status_code == 404:
                        return await super().get_response("index.html", scope)
                    raise

        app.mount("/", SPAStaticFiles(directory=DIST_DIR, html=True), name="ui")
    else:

        @app.get("/")
        def index():
            return {
                "service": "print-intake",
                "hint": "데모 UI 빌드가 없습니다. demo-ui에서 `npm run build` 후 다시 접속하거나, "
                "개발 중에는 vite dev 서버(localhost:5173)를 사용하세요.",
                "api": "/api/health",
            }

    return app


app = create_app()
