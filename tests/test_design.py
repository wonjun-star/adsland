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


def test_parse_freeform_color_bilingual_and_bare_company():
    """라벨 없는 자유형 발화: 값 잘림·회사 감지·색상·영어 병기."""
    c = parse_card_content(
        "명함 만들어줘 피플즈리그 이름은 황원준 직책 수석연구원 "
        "색상은 파란색 영어로도 있음 좋겠고 내 번호는 01046574801",
        None,
    )
    assert c.name == "황원준"
    assert c.title == "수석연구원"          # 지시문이 값에 새지 않는다
    assert c.company == "피플즈리그"        # 라벨 없는 회사명 감지
    assert c.phone == "01046574801"
    assert c.accent_color == "blue"
    assert c.bilingual is True
    assert c.name_en == "Hwang Wonjun"     # 규칙 기반 로마자 (API 없이)
    assert c.title_en == "Senior Researcher"


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_colored_bilingual_card_passes_preflight(tmp_path, template):
    from core.preflight.report import CheckStatus

    content = CardContent(
        name="황원준", title="수석연구원", company="피플즈리그",
        phone="01046574801", accent_color="blue", bilingual=True,
        name_en="Hwang Wonjun", title_en="Senior Researcher",
    )
    out = tmp_path / f"blue_{template}.pdf"
    generate_namecard(content, out, template=template)
    report = run_preflight(out, OrderContext(product="namecard", size_mm=(90, 50), page_count=1))
    assert report.gate_ok, [r.check_id for r in report.results if r.status != CheckStatus.PASS]


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

    # 파일에서 사이즈·인쇄면 추론. 수량·용지·코팅은 버튼으로 물어본다
    assert r.session.slots["size"]["value"] == "90x50"
    assert r.session.slots["sides"]["value"] == "single"
    assert "quantity" in {q.slot for q in r.directives.questions}

    r, reply = chat.process_message(sid, "400장이요")  # 용지·코팅은 아직 안 골라 SLOT_FILLING

    r, reply = chat.process_message(sid, "네 진행할게요")  # 추천값 채우고 확정
    assert r.session.state == "COMPLETED"


def test_double_sided_generates_back(chat):
    """양면 요청 → 앞뒤 2페이지 생성, page_count·sides 정합, 검판 통과."""
    r, _ = chat.start()
    sid = r.session.id
    r, _ = chat.process_message(
        sid, "명함 양면으로 만들어줘 피플즈리그 이름은 황원준 번호는 01046574801"
    )
    assert r.session.slots["sides"]["value"] == "double"
    assert r.directives.report.gate_ok
    pc = r.directives.report.by_id("page_count")
    assert pc.measured["file_pages"] == 2
    # 뒷면에 회사명이 사양 단어 없이 들어간다
    dp = next(c for c in r.cards if c["type"] == "design_preview")
    assert dp["fields"]["company"] == "피플즈리그"


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
