"""질문 정책·에스컬레이션 시그널·3중 관문 유닛테스트 (순수 함수 — DB 없음)."""

from core.orchestrator.policy import (
    BLOCK_CUSTOMER_NOT_CONFIRMED,
    BLOCK_ESCALATED,
    BLOCK_PREFLIGHT_MISSING,
    SIG_HIGH_QUOTE,
    SIG_NEGATIVE_SENTIMENT,
    SIG_PARSE_FAILURES,
    SIG_PREFLIGHT_UNCERTAIN,
    SIG_SLOT_THRASHING,
    SIG_TURNS_EXCEEDED,
    escalation_signals,
    next_actions,
    production_gate,
)
from core.preflight.report import CheckResult, CheckStatus, PreflightReport
from core.products.schema import ProductSchema, Risk, SlotDef


def sticker_schema() -> ProductSchema:
    """PLAN §7 스티커 예시와 같은 구조의 스키마 (catalog 파일에 의존하지 않음)."""
    return ProductSchema(
        product="sticker",
        display_name="스티커",
        slots={
            "size": SlotDef(
                display_name="사이즈",
                required=True,
                infer_from=["file_trimbox"],
                ask_if_conflict=True,
            ),
            "quantity": SlotDef(
                display_name="수량",
                required=True,
                quick_options=[100, 500, 1000],
            ),
            "material": SlotDef(
                display_name="용지",
                required=True,
                default="art_250",
                risk_if_defaulted=Risk.LOW,
            ),
            "coating": SlotDef(
                display_name="코팅",
                required=False,
                default="matte",
                risk_if_defaulted=Risk.LOW,
            ),
            "cut_type": SlotDef(
                display_name="재단 방식",
                required=True,
                infer_from=["dieline_present"],
                default="square",
                risk_if_defaulted=Risk.HIGH,
            ),
        },
    )


def report_with(*statuses: CheckStatus) -> PreflightReport:
    return PreflightReport(
        file="x.pdf",
        results=[
            CheckResult(check_id=f"check_{i}", status=st) for i, st in enumerate(statuses)
        ],
    )


# ---------------------------------------------------------------- 질문 정책 핵심


def test_inferred_size_suppresses_size_question():
    """파일에서 size가 추론되면 size 질문이 생성되지 않는다."""
    d = next_actions(sticker_schema(), slots={}, inferred={"size": "90x50"}, report=None)
    assert "size" not in [q.slot for q in d.questions]


def test_no_inference_asks_size():
    d = next_actions(sticker_schema(), slots={}, inferred={}, report=None)
    assert "size" in [q.slot for q in d.questions]


def test_material_default_low_risk_is_auto_filled_not_asked():
    """default 있고 risk low인 material은 질문 없이 auto_filled."""
    d = next_actions(sticker_schema(), slots={}, inferred={}, report=None)
    assert "material" not in [q.slot for q in d.questions]
    auto = {a.slot: a for a in d.auto_filled}
    assert auto["material"].value == "art_250"
    assert auto["material"].note  # 통보 문구 존재


def test_optional_coating_with_default_auto_filled():
    d = next_actions(sticker_schema(), slots={}, inferred={}, report=None)
    auto = {a.slot: a.value for a in d.auto_filled}
    assert auto.get("coating") == "matte"
    assert "coating" not in [q.slot for q in d.questions]


def test_cut_type_high_risk_asked_despite_default():
    """cut_type은 risk high라 default 있어도 질문."""
    d = next_actions(sticker_schema(), slots={}, inferred={}, report=None)
    q = {q.slot: q for q in d.questions}
    assert "cut_type" in q
    assert q["cut_type"].reason == "required_default_high_risk"
    assert "cut_type" not in [a.slot for a in d.auto_filled]


def test_cut_type_inferred_from_dieline_not_asked():
    d = next_actions(sticker_schema(), slots={}, inferred={"cut_type": "free_cut"}, report=None)
    assert "cut_type" not in [q.slot for q in d.questions]


def test_filled_slot_not_asked_again():
    slots = {"quantity": {"value": 500, "source": "user"}}
    d = next_actions(sticker_schema(), slots=slots, inferred={}, report=None)
    assert "quantity" not in [q.slot for q in d.questions]


def test_question_order_follows_schema_declaration():
    d = next_actions(sticker_schema(), slots={}, inferred={}, report=None)
    # size, quantity, cut_type만 질문 대상이고 순서는 스키마 선언 순
    assert [q.slot for q in d.questions] == ["size", "quantity", "cut_type"]


def test_question_carries_display_name_and_quick_options():
    d = next_actions(sticker_schema(), slots={}, inferred={}, report=None)
    q = {q.slot: q for q in d.questions}
    assert q["quantity"].display_name == "수량"
    assert q["quantity"].quick_options == [100, 500, 1000]
    assert q["size"].reason == "required_no_default"


# ---------------------------------------------------------------- 충돌


def test_conflict_user_vs_inferred_with_ask_if_conflict():
    slots = {"size": {"value": "100x100", "source": "user"}}
    d = next_actions(sticker_schema(), slots=slots, inferred={"size": "90x50"}, report=None)
    assert len(d.conflicts) == 1
    c = d.conflicts[0]
    assert (c.slot, c.user_value, c.inferred_value) == ("size", "100x100", "90x50")
    # 값이 이미 있으므로 일반 질문으로는 안 나간다 (확인은 conflicts 경로)
    assert "size" not in [q.slot for q in d.questions]


def test_no_conflict_when_values_agree():
    slots = {"size": {"value": "90x50", "source": "user"}}
    d = next_actions(sticker_schema(), slots=slots, inferred={"size": "90x50"}, report=None)
    assert d.conflicts == []


def test_no_conflict_when_ask_if_conflict_false():
    schema = sticker_schema()
    schema.slots["size"].ask_if_conflict = False
    slots = {"size": {"value": "100x100", "source": "user"}}
    d = next_actions(schema, slots=slots, inferred={"size": "90x50"}, report=None)
    assert d.conflicts == []


# ---------------------------------------------------------------- 에스컬레이션 시그널 (PLAN §8)


def _signals(**overrides):
    kwargs = dict(
        turn_count=0,
        slot_change_counts={},
        report=None,
        quote_total=None,
        llm_parse_failures=0,
        negative_sentiment=False,
    )
    kwargs.update(overrides)
    return escalation_signals(**kwargs)


def test_no_signals_when_all_nominal():
    assert _signals() == []


def test_turns_boundary():
    assert _signals(turn_count=6) == []                      # 6회까지는 허용
    assert SIG_TURNS_EXCEEDED in _signals(turn_count=7)      # >6회


def test_slot_thrashing_two_or_more_changes():
    assert _signals(slot_change_counts={"size": 1}) == []
    sigs = _signals(slot_change_counts={"size": 2, "quantity": 0})
    assert f"{SIG_SLOT_THRASHING}:size" in sigs
    assert not any(s.endswith(":quantity") for s in sigs)


def test_uncertain_in_report_signals():
    rep = report_with(CheckStatus.PASS, CheckStatus.UNCERTAIN)
    sigs = _signals(report=rep)
    assert any(s.startswith(SIG_PREFLIGHT_UNCERTAIN) for s in sigs)


def test_clean_report_no_signal():
    rep = report_with(CheckStatus.PASS, CheckStatus.WARN)
    assert _signals(report=rep) == []


def test_quote_boundary():
    assert _signals(quote_total=300_000) == []               # 임계값 정확히는 허용
    assert SIG_HIGH_QUOTE in _signals(quote_total=300_001)   # >30만원


def test_parse_failures_two_consecutive():
    assert _signals(llm_parse_failures=1) == []
    assert SIG_PARSE_FAILURES in _signals(llm_parse_failures=2)


def test_negative_sentiment_immediate():
    assert SIG_NEGATIVE_SENTIMENT in _signals(negative_sentiment=True)


def test_multiple_signals_accumulate():
    sigs = _signals(turn_count=10, negative_sentiment=True, quote_total=999_999)
    assert {SIG_TURNS_EXCEEDED, SIG_NEGATIVE_SENTIMENT, SIG_HIGH_QUOTE} <= set(sigs)


# ---------------------------------------------------------------- 3중 관문


def test_gate_ok_when_all_three_pass():
    rep = report_with(CheckStatus.PASS, CheckStatus.PASS)
    g = production_gate(rep, customer_confirmed=True, escalated=False)
    assert g.ok is True
    assert g.blockers == []


def test_gate_blocked_by_uncertain():
    """uncertain 있으면 gate 차단 (해소 전까지)."""
    rep = report_with(CheckStatus.PASS, CheckStatus.UNCERTAIN)
    g = production_gate(rep, customer_confirmed=True, escalated=False)
    assert g.ok is False
    assert any(b.startswith("preflight_uncertain:") for b in g.blockers)


def test_gate_blocked_by_fail():
    rep = report_with(CheckStatus.FAIL)
    g = production_gate(rep, customer_confirmed=True, escalated=False)
    assert g.ok is False
    assert any(b.startswith("preflight_fail:") for b in g.blockers)


def test_gate_warn_does_not_block():
    rep = report_with(CheckStatus.PASS, CheckStatus.WARN)
    g = production_gate(rep, customer_confirmed=True, escalated=False)
    assert g.ok is True


def test_gate_blocked_without_customer_confirm():
    """confirmed 없이 gate 차단."""
    rep = report_with(CheckStatus.PASS)
    g = production_gate(rep, customer_confirmed=False, escalated=False)
    assert g.ok is False
    assert BLOCK_CUSTOMER_NOT_CONFIRMED in g.blockers


def test_gate_blocked_when_escalated():
    rep = report_with(CheckStatus.PASS)
    g = production_gate(rep, customer_confirmed=True, escalated=True)
    assert g.ok is False
    assert BLOCK_ESCALATED in g.blockers


def test_gate_blocked_without_report():
    g = production_gate(None, customer_confirmed=True, escalated=False)
    assert g.ok is False
    assert BLOCK_PREFLIGHT_MISSING in g.blockers


def test_gate_collects_all_blockers():
    rep = report_with(CheckStatus.FAIL, CheckStatus.UNCERTAIN)
    g = production_gate(rep, customer_confirmed=False, escalated=True)
    assert g.ok is False
    assert BLOCK_CUSTOMER_NOT_CONFIRMED in g.blockers
    assert BLOCK_ESCALATED in g.blockers
    assert len(g.blockers) == 4  # fail 1 + uncertain 1 + 미확정 + 에스컬레이션
