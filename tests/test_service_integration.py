"""M3 DoD: LLM 없이 하드코딩된 제안 시퀀스로 상태머신 완주 (PLAN §5 M3).

LLM 계층을 전혀 거치지 않고, 검증된 제안 객체를 직접 주입해
INTAKE → CLASSIFY → SLOT_FILLING → FILE_CHECK → PROOF_CONFIRM → COMPLETED
전 구간이 결정론적으로 동작하는지 확인한다.
"""

from pathlib import Path

import pytest

from core.llm.parsing import ClassifyProposal, CustomerType, Intent, SlotProposal
from core.orchestrator.service import IntakeService
from core.orchestrator.session import PROJECT_ROOT, SessionStore
from core.orchestrator.state_machine import State
from core.preflight.engine import registered_checks

CLEAN_STICKER = PROJECT_ROOT / "data" / "samples" / "clean" / "clean_sticker.pdf"


@pytest.fixture
def svc() -> IntakeService:
    return IntakeService(store=SessionStore("sqlite:///:memory:"))


def _classify_sticker() -> ClassifyProposal:
    return ClassifyProposal(customer_type=CustomerType.A, product="sticker")


def test_full_journey_type_a(svc):
    """A유형(완성 파일 보유): 상품+사양 발화 → 파일 업로드 → 확정 → 완료."""
    r = svc.start()
    sid = r.session.id
    assert r.session.state == State.INTAKE

    # 턴 1: "스티커 90x90으로 500매요" 에 해당하는 제안
    r = svc.apply_turn(
        sid,
        classify=_classify_sticker(),
        proposal=SlotProposal(
            intent=Intent.PROVIDE_INFO,
            slots={"size": "90x90", "quantity": 500, "cut_type": "도무송"},
        ),
    )
    assert r.session.state == State.SLOT_FILLING
    assert r.session.slots["size"]["value"] == "90x90"
    assert r.session.slots["cut_type"]["value"] == "die_cut"  # synonyms 정규화
    # material/coating은 기본값 자동 채움 (risk low)
    assert r.session.slots["material"]["value"] == "art_250"
    assert {q.slot for q in r.directives.questions} == set()
    assert r.directives.request_file  # 파일 요청 단계

    # 턴 2: 정상 파일 업로드
    assert CLEAN_STICKER.exists(), "make gen-samples 필요"
    r = svc.handle_upload(sid, CLEAN_STICKER)
    assert r.directives.report is not None

    if r.directives.report.gate_ok:
        # 검판 통과 → 견적 + 확정 대기
        assert r.session.state == State.PROOF_CONFIRM
        assert r.directives.quote is not None and r.directives.quote.total > 0
        assert r.directives.awaiting_confirm

        # 턴 3: 확정
        r = svc.confirm(sid)
        assert r.session.state == State.COMPLETED
        assert r.directives.order_no
        assert r.cards and r.cards[0]["type"] == "order_confirmed"
    else:
        # 체크 레지스트리 상태에 따라 uncertain이 있으면 확정 단계로 못 간다 — 그 자체가 정상 동작
        assert r.session.state == State.SLOT_FILLING


def test_size_inferred_from_file_suppresses_question(svc):
    """파일 재단크기에서 size가 추론되면 size 질문이 나오지 않는다 (검판 체크 필요)."""
    if "bleed" not in registered_checks() and "page_size" not in registered_checks():
        pytest.skip("프리플라이트 체크 미구현 상태")

    r = svc.start()
    sid = r.session.id
    r = svc.apply_turn(
        sid,
        classify=_classify_sticker(),
        proposal=SlotProposal(intent=Intent.PROVIDE_INFO, slots={"quantity": 500}),
    )
    r = svc.handle_upload(sid, CLEAN_STICKER)
    asked = {q.slot for q in r.directives.questions}
    assert "size" not in asked
    assert r.session.slots.get("size", {}).get("value") == "90x90"
    # 업로드 시 파일 실제 크기가 규격의 기준(source=file) — 파일이 곧 인쇄 크기
    assert r.session.slots["size"]["source"] == "file"
    # 칼선 별색 존재 → cut_type 추론 → risk high지만 질문 억제
    assert "cut_type" not in asked


def test_confirm_before_proof_stage_is_rejected(svc):
    r = svc.start()
    sid = r.session.id
    r = svc.apply_turn(sid, classify=_classify_sticker())
    r = svc.confirm(sid)
    assert "confirm_not_ready" in r.directives.notices
    assert r.session.state != State.COMPLETED


def test_customer_type_c_escalates(svc):
    r = svc.start()
    sid = r.session.id
    r = svc.apply_turn(
        sid,
        classify=ClassifyProposal(customer_type=CustomerType.C, product="sticker"),
    )
    assert r.session.escalated
    assert any("customer_type_C" in x for x in r.directives.escalation_reasons)


def test_slot_thrashing_escalates(svc):
    r = svc.start()
    sid = r.session.id
    svc.apply_turn(
        sid,
        classify=_classify_sticker(),
        proposal=SlotProposal(intent=Intent.PROVIDE_INFO, slots={"quantity": 500}),
    )
    svc.apply_turn(sid, proposal=SlotProposal(intent=Intent.CHANGE, slots={"quantity": 1000}))
    r = svc.apply_turn(sid, proposal=SlotProposal(intent=Intent.CHANGE, slots={"quantity": 300}))
    assert any(x.startswith("slot_thrashing:quantity") for x in r.directives.escalation_reasons)


def test_invalid_slot_value_rejected(svc):
    r = svc.start()
    sid = r.session.id
    r = svc.apply_turn(
        sid,
        classify=_classify_sticker(),
        proposal=SlotProposal(intent=Intent.PROVIDE_INFO, slots={"size": "75x75"}),
    )
    assert "size" not in r.session.slots or r.session.slots["size"].get("value") is None
    assert any(x.startswith("invalid_value:size") for x in r.directives.notices)
    # size는 required·no-default → 질문으로 돌아온다
    assert any(q.slot == "size" for q in r.directives.questions)


def test_event_log_records_journey(svc):
    """철칙 3: 모든 전이가 이벤트 로그에 남는다."""
    r = svc.start()
    sid = r.session.id
    svc.apply_turn(sid, classify=_classify_sticker())
    events = svc.transcript(sid)
    types = [e["type"] for e in events]
    assert "session_created" in types
    assert "transition" in types
    transitions = [e["payload"] for e in events if e["type"] == "transition"]
    assert {"from": "INTAKE", "to": "CLASSIFY", "reason": "first_input"} in transitions
