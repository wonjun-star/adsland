"""명함 시안 생성 — 생성기·파서·플로우 통합."""

import pikepdf
import pytest

from core.design.card import generate_namecard
from core.design.schema import TEMPLATES, CardContent, normalize_phone
from core.llm.roles import extract_template, parse_card_content
from core.orchestrator.chat import ChatPipeline
from core.orchestrator.service import IntakeService
from core.orchestrator.session import SessionStore
from core.preflight.engine import OrderContext, run_preflight


def test_normalize_phone():
    assert normalize_phone("01046574801") == "010-4657-4801"
    assert normalize_phone("021234567") == "02-123-4567"
    assert normalize_phone("0212345678") == "02-1234-5678"


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_generated_card_passes_preflight(tmp_path, template):
    content = CardContent(
        name="황원준", title="수석 연구원", company="피플즈리그 주식회사",
        phone="01046574801", email="wonjun@peoplesleague.co.kr",
    )
    out = tmp_path / f"card_{template}.pdf"
    info = generate_namecard(content, out, template=template)
    assert info["template"] == template

    report = run_preflight(out, OrderContext(product="namecard", size_mm=(90, 50), page_count=1))
    bad = [(r.check_id, r.status.value) for r in report.results if r.status.value != "pass"]
    assert report.gate_ok, bad

    # 한글 폰트 임베딩 확인 (사용된 폰트는 전부 임베딩)
    with pikepdf.open(out) as pdf:
        fonts = dict(pdf.pages[0].get("/Resources", {}).get("/Font", {}))
        for fdict in fonts.values():
            fd = fdict.get("/FontDescriptor", {})
            base = str(fdict.get("/BaseFont", ""))
            if "Nanum" in base:
                assert any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))


def test_parse_card_content_labeled():
    text = ("명함 제작하고 싶고 회사이름: 피플즈리그 주식회사 사람 이름: 황원준 "
            "번호: 01046574801 직위: 수석 연구원 이렇게 넣어서 만들어줘")
    c = parse_card_content(text, None)
    assert c.name == "황원준"
    assert c.company == "피플즈리그 주식회사"
    assert c.title == "수석 연구원"
    assert c.phone == "01046574801"
    assert c.is_generatable()


def test_parse_card_content_ignores_spec_talk():
    assert not parse_card_content("명함 500장 주문할게요", None).is_generatable()
    assert not parse_card_content("회사 이름을 크게 해주세요", None).is_generatable()


def test_extract_template():
    assert extract_template("클래식으로 바꿔줘") == "classic"
    assert extract_template("미니멀하게 해주세요") == "minimal"
    assert extract_template("500장이요") is None


@pytest.fixture
def chat():
    return ChatPipeline(IntakeService(store=SessionStore("sqlite:///:memory:")))


def test_design_flow_full_journey(chat):
    r, _ = chat.start()
    sid = r.session.id

    r, reply = chat.process_message(
        sid,
        "명함 만들고 싶어요. 회사이름: 피플즈리그 주식회사, 이름: 황원준, "
        "직위: 수석 연구원, 번호: 01046574801",
    )
    assert r.session.design_mode
    assert r.session.product == "namecard"
    dp = next(c for c in r.cards if c["type"] == "design_preview")
    assert dp["fields"]["name"] == "황원준"
    assert dp["preview"]  # 미리보기 생성됨
    assert r.directives.report.gate_ok  # 생성 시안이 검판 통과

    # 파일에서 사이즈·인쇄면 추론 → 질문은 수량 1개
    assert r.session.slots["size"]["value"] == "90x50"
    assert r.session.slots["sides"]["value"] == "single"
    assert [q.slot for q in r.directives.questions] == ["quantity"]

    r, reply = chat.process_message(sid, "400장이요")
    assert r.session.state == "PROOF_CONFIRM"
    assert r.directives.quote is not None

    r, reply = chat.process_message(sid, "네 진행할게요")
    assert r.session.state == "COMPLETED"


def test_design_template_switch(chat):
    r, _ = chat.start()
    sid = r.session.id
    chat.process_message(sid, "명함 이름: 김철수 회사: 테스트컴퍼니 주식회사")
    r, _ = chat.process_message(sid, "클래식 스타일로 바꿔줘")
    assert r.session.card_template == "classic"
    dp = next(c for c in r.cards if c["type"] == "design_preview")
    assert dp["template"] == "classic"


def test_uploaded_file_does_not_trigger_design(chat):
    """고객이 파일을 올린 뒤엔 명함이라도 시안 경로로 새지 않는다."""
    from core.orchestrator.session import PROJECT_ROOT

    r, _ = chat.start()
    sid = r.session.id
    chat.process_message(sid, "명함 400장 단면이요")
    pdf = PROJECT_ROOT / "data" / "samples" / "clean" / "clean_namecard.pdf"
    chat.process_upload(sid, pdf, pdf.name)
    # 파일이 있으므로 이름을 말해도 시안 재생성이 아니라 일반 대화로 처리
    r, _ = chat.process_message(sid, "홍길동")
    assert not r.session.design_mode
