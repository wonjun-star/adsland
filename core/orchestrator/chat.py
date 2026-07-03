"""대화 파이프라인 — LLM 계층과 오케스트레이터 서비스를 잇는 접착 계층.

의존 방향 (ADR-001):  llm ← chat → service
llm 모듈(core/llm/roles.py)은 검증된 '제안'과 '문장'만 만들고, 상태 변경은 전부
service(IntakeService)가 한다. 이 모듈은 그 둘을 순서대로 부를 뿐 자체 상태가 없다.

ANTHROPIC_API_KEY가 없으면 adapter_provider가 None을 돌려주고, 모든 역할이
규칙 기반 폴백으로 동일하게 동작한다 — 데모는 키 없이도 완주해야 한다.

LLM 파싱 실패 처리 (PLAN §8: 스키마 검증 실패 2회 연속 → 에스컬레이션 시그널):
    실패 기록 → 1회 재시도 → 또 실패면 규칙 폴백으로 파싱을 이어간다.
    실패 카운트는 세션에 남아 시그널이 되고, 성공하면 리셋된다 ('연속'의 의미 유지).

사용자에게 원문 예외를 노출하는 경로는 없다 — 응답 생성이 실패해도 규칙 템플릿,
그것마저 실패하면 고정 안내 문구로 폴백한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, TypeVar

from core.llm import roles
from core.llm.adapter import LLMAdapter, get_adapter
from core.llm.parsing import ClassifyProposal, ParseError, SlotProposal
from core.orchestrator.service import IntakeService, TurnResult
from core.orchestrator.state_machine import State

T = TypeVar("T")

#: 규칙 템플릿마저 실패했을 때의 최후 안내 (원문 예외는 절대 노출하지 않는다)
_LAST_RESORT_REPLY = "안내 문구를 만드는 중 문제가 생겼어요. 잠시 후 다시 말씀해 주시면 이어서 도와드릴게요."

#: LLM 파싱 시도 횟수 (최초 1회 + 재시도 1회)
_MAX_LLM_ATTEMPTS = 2


class ChatPipeline:
    """자연어 입출력 파이프라인. 반환은 항상 (TurnResult, reply:str) —
    TurnResult 직렬화는 API 계층 몫이고, reply는 그대로 채팅창에 띄우면 된다."""

    def __init__(
        self,
        service: IntakeService,
        adapter_provider: Callable[[], LLMAdapter | None] = get_adapter,
    ):
        self.service = service
        self.adapter_provider = adapter_provider

    # ------------------------------------------------------------ 공개 API

    def start(self) -> tuple[TurnResult, str]:
        """새 세션 시작 + 인사말."""
        result = self.service.start()
        return result, self._render(result, self.adapter_provider())

    def process_message(self, session_id: str, text: str) -> tuple[TurnResult, str]:
        """대화 1턴: (필요시) 분류 → 슬롯 파싱 → 서비스 적용 → 응답 생성."""
        adapter = self.adapter_provider()
        view = self.service.view_session(session_id)

        # 상품 미정일 때만 분류기를 부른다 (정해진 상품을 매 턴 재분류하지 않는다)
        classify: ClassifyProposal | None = None
        if not view.product:
            classify = self._propose(
                session_id,
                adapter,
                llm=lambda: roles.classify_input(text, self.service.catalog, adapter),
                rule=lambda: roles.classify_input(text, self.service.catalog, None),
            )

        product = view.product
        if not product and classify is not None and classify.product in self.service.catalog:
            product = classify.product
        schema = self.service.catalog.get(product) if product else None
        awaiting = view.state == State.PROOF_CONFIRM.value

        proposal: SlotProposal = self._propose(
            session_id,
            adapter,
            llm=lambda: roles.parse_slots(text, schema, adapter, awaiting_confirm=awaiting),
            rule=lambda: roles.parse_slots(text, schema, None, awaiting_confirm=awaiting),
        )

        result = self.service.apply_turn(session_id, classify=classify, proposal=proposal)
        return result, self._render(result, adapter)

    def process_upload(
        self, session_id: str, saved_path: str | Path, original_name: str = ""
    ) -> tuple[TurnResult, str]:
        """파일 업로드 → 검판 → 응답 생성. saved_path는 API 계층이 이미 저장해 둔 경로."""
        result = self.service.handle_upload(session_id, saved_path, original_name)
        return result, self._render(result, self.adapter_provider())

    def process_autofix(self, session_id: str, check_id: str) -> tuple[TurnResult, str]:
        """자동 보정 적용 → 재검판 → 응답 생성."""
        result = self.service.handle_autofix(session_id, check_id)
        return result, self._render(result, self.adapter_provider())

    def process_confirm(self, session_id: str) -> tuple[TurnResult, str]:
        """확정 버튼 경로 (자연어 '네 진행해주세요'는 process_message가 처리)."""
        result = self.service.confirm(session_id)
        return result, self._render(result, self.adapter_provider())

    # ------------------------------------------------------------ 내부

    def _propose(
        self,
        session_id: str,
        adapter: LLMAdapter | None,
        llm: Callable[[], T],
        rule: Callable[[], T],
    ) -> T:
        """LLM 제안 시도 + 실패 카운트 관리. 규칙 폴백은 결정론이라 실패하지 않는다."""
        if adapter is None:
            return rule()
        for _ in range(_MAX_LLM_ATTEMPTS):
            try:
                proposal = llm()
            except ParseError:
                # 검증 실패는 세션에 누적 — policy의 llm_parse_failures 시그널 입력이 된다
                self.service.store.record_llm_parse_failure(session_id)
                continue
            self.service.store.reset_llm_parse_failures(session_id)
            return proposal
        return rule()  # 2회 연속 실패 → 규칙 폴백으로 계속 진행 (카운트는 시그널로 남음)

    def _render(self, result: TurnResult, adapter: LLMAdapter | None) -> str:
        """directives → 응답 문장. 어떤 실패도 사용자에게 원문 예외로 노출하지 않는다."""
        schema = (
            self.service.catalog.get(result.session.product) if result.session.product else None
        )
        if adapter is not None:
            try:
                return roles.render_reply(result.directives, result.session, schema, adapter)
            except Exception:
                pass  # LLM 생성 실패 → 규칙 템플릿 폴백
        try:
            return roles.render_reply(result.directives, result.session, schema, None)
        except Exception:
            return _LAST_RESORT_REPLY
