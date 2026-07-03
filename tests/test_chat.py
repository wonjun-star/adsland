"""ChatPipeline 테스트 — 자연어 입출력으로 상태머신 완주 (M4).

기본은 adapter=None(규칙 폴백, 결정론). LLM 경로는 가짜 어댑터로 네트워크 없이 검증:
  - 항상 깨진 JSON을 뱉는 어댑터 → 실패 기록 → 재시도 → 규칙 폴백 + 에스컬레이션 시그널
  - 유효한 JSON을 뱉는 어댑터 → LLM 제안이 적용되고 실패 카운터가 리셋
  - dialog에서 예외를 던지는 어댑터 → 규칙 템플릿 폴백 (원문 예외 비노출)
"""

from __future__ import annotations

import pytest

from core.orchestrator.chat import ChatPipeline
from core.orchestrator.service import IntakeService
from core.orchestrator.session import PROJECT_ROOT, SessionStore
from core.orchestrator.state_machine import State

CLEAN_STICKER = PROJECT_ROOT / "data" / "samples" / "clean" / "clean_sticker.pdf"


def _pipeline(adapter_provider=lambda: None) -> ChatPipeline:
    svc = IntakeService(store=SessionStore("sqlite:///:memory:"))
    return ChatPipeline(svc, adapter_provider=adapter_provider)


# ---------------------------------------------------------------- 완주 시나리오 (규칙 폴백)


def test_full_journey_rule_fallback():
    """키 없이(adapter=None) 스티커 문의 → 사양 발화 → 업로드 → 확정 → COMPLETED."""
    assert CLEAN_STICKER.exists(), "make gen-samples 필요"
    pipe = _pipeline()

    r, reply = pipe.start()
    sid = r.session.id
    assert r.session.state == State.INTAKE
    assert "안녕하세요" in reply

    # 턴 1: 상품 + 사양 발화 (분류기 + 슬롯 파서 규칙 폴백)
    r, reply = pipe.process_message(sid, "스티커 문의드려요. 90x90으로 500매, 도무송으로 부탁드려요.")
    assert r.session.state == State.SLOT_FILLING
    assert r.session.product == "sticker"
    assert r.session.slots["size"]["value"] == "90x90"
    assert r.session.slots["quantity"]["value"] == 500
    assert r.session.slots["cut_type"]["value"] == "die_cut"
    # material/coating은 기본값 자동 채움(risk low) → 통보 문장
    assert r.session.slots["material"]["value"] == "art_250"
    assert r.directives.request_file
    assert isinstance(reply, str) and "파일" in reply

    # 턴 2: 정상 파일 업로드 → 검판 통과 → 견적 + 확정 대기
    r, reply = pipe.process_upload(sid, CLEAN_STICKER, "clean_sticker.pdf")
    assert r.directives.report is not None
    assert r.directives.report.gate_ok
    assert r.session.state == State.PROOF_CONFIRM
    assert r.directives.awaiting_confirm
    assert r.directives.quote is not None and r.directives.quote.total > 0
    assert "이대로 진행할까요?" in reply
    assert "부가세 포함" in reply

    # 턴 3: 자연어 확정 → COMPLETED
    r, reply = pipe.process_message(sid, "네 이대로 진행해주세요")
    assert r.session.state == State.COMPLETED
    assert r.directives.order_no
    assert r.directives.order_no in reply
    assert not r.session.escalated


def test_process_confirm_button_path():
    """버튼 확정 경로(process_confirm)도 같은 결과에 도달한다."""
    pipe = _pipeline()
    r, _ = pipe.start()
    sid = r.session.id
    pipe.process_message(sid, "스티커 90x90 500매 도무송이요")
    r, _ = pipe.process_upload(sid, CLEAN_STICKER)
    assert r.session.state == State.PROOF_CONFIRM
    r, reply = pipe.process_confirm(sid)
    assert r.session.state == State.COMPLETED
    assert r.directives.order_no and r.directives.order_no in reply


def test_upload_before_product_asks_for_product():
    pipe = _pipeline()
    r, _ = pipe.start()
    sid = r.session.id
    r, reply = pipe.process_upload(sid, CLEAN_STICKER)
    assert r.directives.request_product
    assert "상품" in reply


def test_autofix_without_file_is_graceful():
    pipe = _pipeline()
    r, _ = pipe.start()
    sid = r.session.id
    pipe.process_message(sid, "스티커요")
    r, reply = pipe.process_autofix(sid, "bleed")
    assert "autofix_no_file" in r.directives.notices
    assert isinstance(reply, str) and "autofix_no_file" not in reply  # 기계 코드 비노출


# ---------------------------------------------------------------- LLM 경로 (가짜 어댑터)


class GarbageAdapter:
    """분류/파싱에 항상 깨진 출력 — 검증 실패 경로 검증용. dialog는 정상 문장."""

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, system, messages, role="dialog", max_tokens=1024, temperature=0.2):
        self.calls.append(role)
        if role == "dialog":
            return "네, 확인했어요."
        return "이건 JSON이 아닙니다"


class ScriptedAdapter:
    """역할별로 유효한 JSON을 돌려주는 어댑터 — LLM 제안 적용 경로 검증용."""

    def complete(self, system, messages, role="dialog", max_tokens=1024, temperature=0.2):
        if role == "classify":
            return '{"customer_type": "A", "product": "sticker", "confidence_signals": ["테스트"]}'
        if role == "parse":
            return '```json\n{"intent": "provide_info", "slots": {"quantity": 300}}\n```'
        return "상담원 응답입니다."


class BrokenDialogAdapter(ScriptedAdapter):
    """대화 생성만 실패 — 규칙 템플릿 폴백 검증용."""

    def complete(self, system, messages, role="dialog", max_tokens=1024, temperature=0.2):
        if role == "dialog":
            raise RuntimeError("모델 응답 없음")
        return super().complete(system, messages, role, max_tokens, temperature)


def test_llm_parse_failures_fall_back_and_escalate():
    """깨진 LLM 출력: 실패 기록 → 재시도 → 규칙 폴백. 카운트는 시그널이 된다 (PLAN §8)."""
    adapter = GarbageAdapter()
    pipe = _pipeline(adapter_provider=lambda: adapter)
    r, _ = pipe.start()
    sid = r.session.id

    r, reply = pipe.process_message(sid, "스티커 500매 부탁해요")
    # 분류 2회 + 파싱 2회 실패 → 재시도 흔적
    assert adapter.calls.count("classify") == 2
    assert adapter.calls.count("parse") == 2
    # 규칙 폴백이 이어받아 정상 진행
    assert r.session.product == "sticker"
    assert r.session.slots["quantity"]["value"] == 500
    # 실패 2회 연속 → 에스컬레이션 시그널
    assert any("llm_parse_failures" in x for x in r.directives.escalation_reasons)
    assert isinstance(reply, str) and reply


def test_llm_success_applies_proposal_and_resets_counter():
    pipe = _pipeline(adapter_provider=lambda: ScriptedAdapter())
    r, _ = pipe.start()
    sid = r.session.id
    r, reply = pipe.process_message(sid, "스티커 300장이요")
    assert r.session.product == "sticker"
    assert r.session.slots["quantity"]["value"] == 300  # LLM 제안이 적용됨
    assert pipe.service.store.get(sid).llm_parse_failures == 0
    assert reply == "상담원 응답입니다."


def test_render_failure_falls_back_to_rule_template():
    pipe = _pipeline(adapter_provider=lambda: BrokenDialogAdapter())
    r, reply = pipe.start()
    sid = r.session.id
    assert isinstance(reply, str) and "안녕하세요" in reply  # 규칙 템플릿 폴백

    r, reply = pipe.process_message(sid, "스티커요")
    assert isinstance(reply, str) and reply
    assert "RuntimeError" not in reply and "모델 응답 없음" not in reply  # 원문 예외 비노출
