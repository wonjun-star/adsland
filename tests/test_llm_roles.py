"""LLM 역할 3종의 규칙 폴백 테스트 — 전부 adapter=None (결정론, 네트워크 없음).

M4 계약: ANTHROPIC_API_KEY가 없어도 분류/파싱/응답 생성이 동일 인터페이스로 완주한다.
여기서 고정하는 규칙:
  - 같은 슬롯에 여러 synonyms가 매칭되면 발화에서 **뒤에 나온 표현이 승자**
    ("도톰한 방수 스티커" → material=pvc_white)
  - 리포트 번역은 measured 숫자를 반드시 문장에 포함한다 (철칙 2 — LLM/템플릿은 번역가)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.llm.parsing import CustomerType, Intent
from core.llm.roles import classify_input, parse_slots, render_reply, translate_check
from core.products.schema import load_catalog

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CATALOG = load_catalog()
STICKER = CATALOG["sticker"]


# ---------------------------------------------------------------- 의존 방향 (ADR-001)


def test_llm_layer_never_imports_orchestrator():
    """core/llm은 오케스트레이터/DB를 런타임에 임포트하지 않는다."""
    code = (
        "import sys; import core.llm.roles; "
        "bad = [m for m in sys.modules if m.startswith('core.orchestrator')]; "
        "assert not bad, f'llm이 오케스트레이터를 임포트함: {bad}'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------- 분류기


def test_classify_product_and_default_type_a():
    p = classify_input("스티커 문의드려요", CATALOG, adapter=None)
    assert p.product == "sticker"
    assert p.customer_type == CustomerType.A


def test_classify_flyer_aliases():
    for text in ("전단지 뽑고 싶어요", "전단 인쇄요", "리플렛 문의합니다"):
        assert classify_input(text, CATALOG, adapter=None).product == "flyer"


def test_classify_customer_type_c_design_request():
    p = classify_input("명함 시안 만들어 주세요", CATALOG, adapter=None)
    assert p.product == "namecard"
    assert p.customer_type == CustomerType.C


def test_classify_customer_type_b_fix_request():
    p = classify_input("전단지 파일 수정해 주실 수 있나요", CATALOG, adapter=None)
    assert p.product == "flyer"
    assert p.customer_type == CustomerType.B


def test_classify_unsupported_product_is_flagged():
    """애즈랜드가 팔지만 아직 없는 상품(슬로건·어깨띠)은 'slogan'으로 인식 — 조용히 무시하지 않고
    오케스트레이터가 '준비 중'으로 안내하게 한다. 카탈로그엔 없으므로 unknown_product 처리."""
    p = classify_input("슬로건 되나요?", CATALOG, adapter=None)
    assert p.product == "slogan"
    assert p.product not in CATALOG
    assert p.customer_type == CustomerType.A


def test_classify_banner_now_supported():
    """현수막·배너는 이제 정식 지원 상품 — 카탈로그의 banner 로 분류된다(더 이상 미지원 안내 아님)."""
    for text in ("현수막 만들래요", "엑스배너 뽑고 싶어요", "실사출력 문의드려요"):
        p = classify_input(text, CATALOG, adapter=None)
        assert p.product == "banner", text
        assert p.product in CATALOG


def test_classify_truly_unknown_is_none():
    p = classify_input("우주선 되나요?", CATALOG, adapter=None)
    assert p.product is None


# ---------------------------------------------------------------- 슬롯 파서


def test_parse_later_expression_wins_and_units():
    """'도톰한'(art_300)보다 뒤에 나온 '방수'(pvc_white)가 material 승자."""
    p = parse_slots("도톰한 방수 스티커 500매 무광으로", STICKER, adapter=None)
    assert p.slots["material"] == "pvc_white"
    assert p.slots["quantity"] == 500
    assert p.slots["coating"] == "matte"
    assert p.intent == Intent.PROVIDE_INFO


def test_parse_size_and_korean_thousand():
    p = parse_slots("90x90으로 천 장", STICKER, adapter=None)
    assert p.slots["size"] == "90x90"
    assert p.slots["quantity"] == 1000


@pytest.mark.parametrize("text", ["90X90", "90×90", "90 * 90", "90x90"])
def test_parse_size_pattern_variants(text):
    p = parse_slots(f"{text} 사이즈요", STICKER, adapter=None)
    assert p.slots["size"] == "90x90"


def test_parse_korean_number_variants():
    assert parse_slots("1,000매 부탁드려요", STICKER, adapter=None).slots["quantity"] == 1000
    assert parse_slots("만 장 정도요", STICKER, adapter=None).slots["quantity"] == 10000
    assert parse_slots("5천 장이요", STICKER, adapter=None).slots["quantity"] == 5000


def test_parse_longest_synonym_first():
    """'코팅 없이'(none)가 부분 문자열보다 먼저 잡힌다."""
    p = parse_slots("코팅 없이 해주세요", STICKER, adapter=None)
    assert p.slots["coating"] == "none"


def test_parse_dieline_jargon():
    p = parse_slots("도무송으로 따주세요", STICKER, adapter=None)
    assert p.slots["cut_type"] == "die_cut"


def test_intent_confirm_only_when_awaiting():
    p = parse_slots("네 이대로 진행해주세요", STICKER, adapter=None, awaiting_confirm=True)
    assert p.intent == Intent.CONFIRM
    p2 = parse_slots("네 이대로 진행해주세요", STICKER, adapter=None, awaiting_confirm=False)
    assert p2.intent != Intent.CONFIRM


def test_intent_change_beats_confirm():
    p = parse_slots("아니요 무광 말고 유광으로 바꿔주세요", STICKER, adapter=None, awaiting_confirm=True)
    assert p.intent == Intent.CHANGE
    assert p.slots["coating"] == "gloss"  # '말고' 뒤의 '유광'이 승자


def test_intent_deny():
    p = parse_slots("취소할게요", STICKER, adapter=None, awaiting_confirm=True)
    assert p.intent == Intent.DENY


def test_intent_complaint_sets_negative_sentiment():
    p = parse_slots("배송이 왜 이렇게 늦어요 짜증나네요", STICKER, adapter=None)
    assert p.intent == Intent.COMPLAINT
    assert p.negative_sentiment


def test_intent_question():
    p = parse_slots("500매면 얼마예요?", STICKER, adapter=None)
    assert p.intent == Intent.QUESTION
    assert p.slots["quantity"] == 500  # 질문이어도 값 제안은 유지


def test_parse_without_schema_still_extracts_numbers():
    p = parse_slots("70x70으로 300개요", None, adapter=None)
    assert p.slots["size"] == "70x70"
    assert p.slots["quantity"] == 300


# ---------------------------------------------------------------- 대화 생성기 (규칙 템플릿)
# 렌더 재료(ReplyDirectives 등)는 오케스트레이터 소속이므로 테스트에서만 임포트한다.

from core.orchestrator.policy import AutoFill, SlotQuestion  # noqa: E402
from core.orchestrator.service import ReplyDirectives, SessionView  # noqa: E402
from core.preflight.report import AutofixInfo, CheckResult, CheckStatus, PreflightReport  # noqa: E402
from core.quote.engine import QuoteLine, QuoteResult  # noqa: E402


def _view(**kw) -> SessionView:
    base = dict(id="s1", state="SLOT_FILLING", product="sticker")
    base.update(kw)
    return SessionView(**base)


def _bleed_fail() -> CheckResult:
    return CheckResult(
        check_id="bleed",
        status=CheckStatus.FAIL,
        measured={"min_mm": 0.0, "insets_mm": {"left": 0, "right": 0, "top": 0, "bottom": 0}},
        required={"min_mm": 3.0},
        autofix=AutofixInfo(available=True, fix_id="extend_bleed"),
    )


def test_render_bleed_fail_summary_and_detail_in_translation():
    """결과 우선: 챗은 한 줄 요약, 상세(기준·실측)는 카드가 쓰는 translate_check에 보존."""
    from core.llm.roles import translate_check

    d = ReplyDirectives(
        kind="upload",
        report=PreflightReport(file="x.pdf", results=[_bleed_fail()]),
        offer_autofix=["bleed"],
    )
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "검판 완료" in reply and "1건" in reply
    assert "여백" in reply and "바꿔드릴" in reply  # 자동 보정 제안
    detail = translate_check(_bleed_fail())  # 상세 숫자는 카드용 번역에 그대로
    assert "3mm" in detail and "0mm" in detail


def test_render_resolution_fail_detail_in_translation():
    from core.llm.roles import translate_check

    r = CheckResult(
        check_id="resolution",
        status=CheckStatus.FAIL,
        measured={"min_dpi": 96.0, "images": [{"page": 0, "name": "Im0", "dpi": 96.0}]},
        required={"pass_dpi": 300, "fail_below": 150},
    )
    d = ReplyDirectives(kind="upload", report=PreflightReport(file="x.pdf", results=[r]))
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "검판 완료" in reply           # 챗은 요약
    detail = translate_check(r)
    assert "96dpi" in detail and "300dpi" in detail and "흐릿" in detail


def test_translate_check_covers_all_12_checks_and_4_statuses():
    """12개 체크 × 4개 상태 전 조합이 빈 문장 없이 한국어로 번역된다."""
    check_ids = [
        "bleed", "resolution", "colorspace", "font_embed", "trim_safety", "ink_total",
        "black_type", "page_size", "page_count", "transparency", "dieline", "min_line",
    ]
    for cid in check_ids:
        for status in CheckStatus:
            r = CheckResult(check_id=cid, status=status)
            line = translate_check(r)
            assert isinstance(line, str) and len(line) >= 5, f"{cid}/{status} 문장 없음"
            if status == CheckStatus.UNCERTAIN:
                assert "담당자" in line or "확인" in line, f"{cid}/uncertain 에스컬레이션 안내 없음"


def test_render_greeting():
    d = ReplyDirectives(kind="greeting", request_product=True, request_file=True)
    reply = render_reply(d, _view(product=None, state="INTAKE"), None, adapter=None)
    assert "안녕하세요" in reply


def test_render_quote_vat_included():
    q = QuoteResult(
        product="sticker",
        supply_amount=22000,
        vat=2200,
        total=24200,
        lines=[QuoteLine(item="base", description="기본 인쇄비", amount=22000)],
    )
    d = ReplyDirectives(kind="turn", quote=q)
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "부가세 포함" in reply
    assert "24,200원" in reply


def test_render_questions_with_quick_options():
    d = ReplyDirectives(
        kind="turn",
        questions=[SlotQuestion(slot="quantity", display_name="수량", quick_options=[100, 500, 1000])],
    )
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "100매/500매/1000매" in reply
    assert "골라" in reply


def test_render_auto_filled_not_narrated():
    """결과 우선: 자동 채운 값은 사이드 요약·슬롯에 있으니 챗에서 반복하지 않는다(간결)."""
    d = ReplyDirectives(kind="turn", auto_filled=[AutoFill(slot="material", value="art_250")])
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "기본 적용" not in reply  # 수다스러운 통보 제거
    assert len(reply) < 40           # 다른 지시가 없으면 아주 짧다


def test_render_offer_final_review_asks_first():
    """사양 완료 시 계약서(견적 카드)를 들이밀지 않고 '더 바꿀 내용?'을 먼저 묻는다."""
    view = _view(
        state="PROOF_CONFIRM",
        slots={
            "size": {"value": "90x90", "source": "user"},
            "quantity": {"value": 500, "source": "user"},
            "material": {"value": "art_250", "source": "default"},
        },
    )
    d = ReplyDirectives(kind="turn", awaiting_confirm=True, offer_final_review=True)
    reply = render_reply(d, view, STICKER, adapter=None)
    assert "더 바꾸실" in reply or "최종 견적" in reply
    assert "이대로 진행할까요?" not in reply  # 계약서 재촉 안 함


def test_render_show_final_points_to_card():
    """고객이 최종 견적을 요청하면 카드로 안내한다 (바꾸기/이대로 주문)."""
    d = ReplyDirectives(kind="turn", awaiting_confirm=True, show_final=True)
    reply = render_reply(d, _view(state="PROOF_CONFIRM"), STICKER, adapter=None)
    assert "이대로 주문" in reply or "바꾸기" in reply


def test_render_estimate_quote_prefix():
    from core.quote.engine import QuoteResult

    q = QuoteResult(product="sticker", total=24200, supply_amount=22000, vat=2200)
    d = ReplyDirectives(kind="turn", quote=q, estimate=True)
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "예상 견적" in reply and "24,200원" in reply


def test_render_conflict_question():
    from core.orchestrator.policy import Conflict

    d = ReplyDirectives(
        kind="turn",
        conflicts=[Conflict(slot="size", display_name="사이즈", user_value="70x70", inferred_value="90x90")],
    )
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "90x90" in reply and "70x70" in reply
    assert "어느 쪽" in reply


def test_render_escalation_and_order_no():
    d = ReplyDirectives(kind="turn", escalation_reasons=["preflight_uncertain:dieline"])
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "담당자 검토" in reply

    d2 = ReplyDirectives(kind="confirm", order_no="PL-ABCD1234")
    reply2 = render_reply(d2, _view(state="COMPLETED"), STICKER, adapter=None)
    assert "PL-ABCD1234" in reply2


def test_render_never_exposes_machine_codes():
    d = ReplyDirectives(kind="turn", notices=["invalid_value:size=75"], request_product=False)
    reply = render_reply(d, _view(), STICKER, adapter=None)
    assert "invalid_value" not in reply
    assert "75" in reply  # 값 자체는 인용해서 안내
