"""API 계층 테스트 — docs/API.md 계약 (TestClient, 인메모리 DB).

검증 범위:
  - /api/health
  - 세션 생성 → 메시지 → 정상 PDF 업로드 → 확정 완주 (HTTP만으로 상태머신 완주)
  - ACCESS_CODE 설정 시 401 / 코드 통과 후 쿠키 인증
  - 비PDF 업로드 거부, /api/files 경로 탈출 차단
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.orchestrator.session import PROJECT_ROOT

CLEAN_STICKER = PROJECT_ROOT / "data" / "samples" / "clean" / "clean_sticker.pdf"


def _make_client(monkeypatch, access_code: str | None = None) -> TestClient:
    """인메모리 DB로 앱을 새로 만든 TestClient. 파일 DB를 건드리지 않는다."""
    monkeypatch.setenv("PRINT_INTAKE_DB", "sqlite:///:memory:")
    if access_code is None:
        monkeypatch.delenv("ACCESS_CODE", raising=False)
    else:
        monkeypatch.setenv("ACCESS_CODE", access_code)
    from api.main import create_app

    return TestClient(create_app())


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    return _make_client(monkeypatch)


# ---------------------------------------------------------------- 헬스


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------- 완주 시나리오


def test_full_journey_over_http(client):
    """세션 생성 → 사양 발화 → 정상 파일 업로드 → 확정. UI가 쓸 응답 형태 그대로 검증."""
    assert CLEAN_STICKER.exists(), "make gen-samples 필요"

    # 1) 세션 생성 + 인사말
    r = client.post("/api/session")
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"session", "reply", "cards"}
    sid = data["session"]["id"]
    assert data["session"]["state"] == "INTAKE"
    assert "안녕하세요" in data["reply"]["text"]
    assert isinstance(data["reply"]["quick_options"], list)

    # 2) 상품 + 사양 발화 (규칙 폴백 파싱)
    r = client.post(
        f"/api/session/{sid}/message",
        json={"text": "스티커 문의드려요. 90x90으로 500매, 도무송으로 부탁드려요."},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["session"]["state"] == "SLOT_FILLING"
    assert data["session"]["product"] == "sticker"
    assert data["session"]["slots"]["size"]["value"] == "90x90"
    assert data["session"]["slots"]["quantity"]["value"] == 500
    assert "파일" in data["reply"]["text"]

    # 3) 정상 PDF 업로드 → 검판 리포트 + 미리보기 카드 + 견적
    with open(CLEAN_STICKER, "rb") as f:
        r = client.post(
            f"/api/session/{sid}/upload",
            files={"file": ("clean_sticker.pdf", f, "application/pdf")},
        )
    assert r.status_code == 200
    data = r.json()
    # 용지·코팅을 아직 안 골라 SLOT_FILLING (검판은 통과). 확정 시 추천값으로 채워 완료된다.
    # 슬롯 채우는 중엔 예상 견적 카드를 보여줘 결정을 돕는다(확정 단계의 계약서식 반복과 다름).
    assert data["session"]["state"] == "SLOT_FILLING"
    types = [c["type"] for c in data["cards"]]
    assert "preflight_report" in types
    report = next(c for c in data["cards"] if c["type"] == "preflight_report")
    assert report["gate_ok"] is True

    # file_preview 카드: 서버가 렌더한 1페이지 PNG가 /api/files/ 로 서빙된다
    preview = next(c for c in data["cards"] if c["type"] == "file_preview")
    assert preview["url"].startswith("/api/files/")
    img = client.get(preview["url"])
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"

    # 4) 확정 → COMPLETED + order_confirmed 카드
    r = client.post(f"/api/session/{sid}/confirm")
    assert r.status_code == 200
    data = r.json()
    assert data["session"]["state"] == "COMPLETED"
    confirmed = next(c for c in data["cards"] if c["type"] == "order_confirmed")
    assert confirmed["order_no"]
    assert confirmed["order_no"] in data["reply"]["text"]

    # 5) 세션 조회: 최신 스냅샷 + 이벤트 로그(transcript)
    r = client.get(f"/api/session/{sid}")
    assert r.status_code == 200
    data = r.json()
    assert data["session"]["state"] == "COMPLETED"
    event_types = [e["type"] for e in data["transcript"]]
    assert "session_created" in event_types
    assert "preflight_report" in event_types
    assert "customer_confirmed" in event_types


def test_unknown_session_is_404(client):
    r = client.post("/api/session/없는세션/message", json={"text": "안녕하세요"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 접속 제어


def test_access_code_gate(monkeypatch):
    """ACCESS_CODE 설정 시: 코드 없으면 401, 통과하면 쿠키로 계속 인증."""
    client = _make_client(monkeypatch, access_code="demo-2026")

    # 예외 경로는 열려 있다
    assert client.get("/api/health").status_code == 200

    # 쿠키 없이 보호 API → 401
    assert client.post("/api/session").status_code == 401
    assert client.get("/api/demo/board").status_code == 401

    # 틀린 코드 → 401, 쿠키 미발급
    r = client.post("/api/access", json={"code": "틀린코드"})
    assert r.status_code == 401
    assert client.post("/api/session").status_code == 401

    # 맞는 코드 → 쿠키 발급 → 이후 요청 통과
    r = client.post("/api/access", json={"code": "demo-2026"})
    assert r.status_code == 200
    r = client.post("/api/session")
    assert r.status_code == 200
    assert "안녕하세요" in r.json()["reply"]["text"]

    # 위조 쿠키는 거부된다 (서명 검사)
    forged = _make_client(monkeypatch, access_code="demo-2026")
    forged.cookies.set("pi_access", "0" * 64)
    assert forged.post("/api/session").status_code == 401


def test_no_access_code_means_open(client):
    """ACCESS_CODE 미설정이면 전부 개방 (로컬 개발)."""
    assert client.post("/api/session").status_code == 200
    assert client.get("/api/demo/board").status_code == 200


# ---------------------------------------------------------------- 업로드 방어


def test_non_pdf_upload_rejected(client):
    sid = client.post("/api/session").json()["session"]["id"]
    r = client.post(
        f"/api/session/{sid}/upload",
        files={"file": ("innocent.pdf", b"MZ this is not a pdf", "application/pdf")},
    )
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_files_endpoint_blocks_path_escape(client):
    # URL 인코딩된 ../ 로 previews 밖(업로드 원본 등)을 노리는 요청 → 404
    r = client.get("/api/files/..%2F..%2Fpyproject.toml")
    assert r.status_code == 404
    r = client.get("/api/files/..%5Cuploads%5Cx.pdf")
    assert r.status_code == 404


# ---------------------------------------------------------------- 데모 게시판


def test_demo_board_thread_shape(client):
    """왼쪽 패널용 합성 스레드: 왕복 6게시글 + 반나절~1일 경과 연출."""
    r = client.get("/api/demo/board")
    assert r.status_code == 200
    data = r.json()
    posts = data["posts"]
    assert len(posts) == 6
    assert [p["role"] for p in posts] == ["customer", "staff"] * 3
    # 고객 문의(첨부)로 시작해서 직원 최종 확인으로 끝난다
    assert posts[0]["attachment"]
    assert posts[-1]["role"] == "staff"
    # 시각이 이틀에 걸쳐 있어 시간 소요가 드러난다
    assert "09:12" in posts[0]["time"]
    assert posts[0]["time"][:10] != posts[-1]["time"][:10]
    assert data["total_elapsed"]
