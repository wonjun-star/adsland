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

from core.design.schema import DESIGNABLE_PRODUCTS, CardContent
from core.llm import roles
from core.llm.adapter import LLMAdapter, get_adapter
from core.llm.parsing import ClassifyProposal, CustomerType, Intent, ParseError, SlotProposal
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
        """대화 1턴: (필요시) 분류 → 슬롯 파싱 → 서비스 적용 → 응답 생성.

        명함 시안 경로 분기: 파일 없는 명함 고객이 내용을 주거나 시안 제작을 요청하면
        슬롯 파싱 대신 명함 내용 파서로 넘겨 시안 생성 흐름을 탄다.
        """
        adapter = self.adapter_provider()
        view = self.service.view_session(session_id)

        # 상품 미정이면 분류기로 추정. 이미 정해졌어도 고객이 '다른 상품'을 명시하면
        # 교체를 허용한다 (파일 기반 추정이 틀렸을 때 "아니 엽서야"로 바로잡는 경로).
        classify: ClassifyProposal | None = None
        if not view.product:
            classify = self._propose(
                session_id,
                adapter,
                llm=lambda: roles.classify_input(text, self.service.catalog, adapter),
                rule=lambda: roles.classify_input(text, self.service.catalog, None),
            )
        else:
            named = roles.classify_input(text, self.service.catalog, None)  # 규칙 기반 상품 키워드
            if named.product and named.product != view.product:
                classify = named  # 명시적 상품 변경 → apply_turn이 교체·재검판

        product = view.product
        if classify is not None and classify.product in self.service.catalog:
            product = classify.product

        # 시안 경로 분기: 명함 + 파일 없음 + (이미 시안 모드 ∨ 내용 제공 ∨ 시안 제작 요청)
        design_route = self._maybe_design(session_id, text, adapter, classify, view, product)
        if design_route is not None:
            return design_route

        schema = self.service.catalog.get(product) if product else None
        awaiting = view.state == State.PROOF_CONFIRM.value

        proposal: SlotProposal = self._propose(
            session_id,
            adapter,
            llm=lambda: roles.parse_slots(text, schema, adapter, awaiting_confirm=awaiting),
            rule=lambda: roles.parse_slots(text, schema, None, awaiting_confirm=awaiting),
        )

        # 고객이 방금 한 말 원문을 서비스에 넘긴다 — 정해진 경로에 안 맞는 요청(옵션별 비교,
        # "용지 뭐 있어?" 등)도 의도를 읽어 선택지·안내를 만든다. 숫자는 directives 값만 쓴다.
        result = self.service.apply_turn(
            session_id, classify=classify, proposal=proposal, customer_text=text
        )
        # 명시적 질문이면(용지·사이즈·가격 등) '답 먼저' 흐름을 태운다
        if proposal is not None and proposal.intent == Intent.QUESTION:
            result.directives.customer_question = text
        return result, self._render(result, adapter)

    def _maybe_design(
        self,
        session_id: str,
        text: str,
        adapter: LLMAdapter | None,
        classify: ClassifyProposal | None,
        view,
        product: str | None,
    ) -> tuple[TurnResult, str] | None:
        """명함 시안 경로 여부 판단 후, 맞으면 내용 파싱→생성까지 처리하고 반환."""
        # 우리가 만든 시안(design_mode)은 계속 편집 허용. 고객이 올린 파일이면 시안 경로 진입 금지.
        if product not in DESIGNABLE_PRODUCTS or (view.file_path and not view.design_mode):
            return None

        design_ask = classify is not None and classify.customer_type == CustomerType.C
        content = self._propose(
            session_id,
            adapter,
            llm=lambda: roles.parse_card_content(text, adapter),
            rule=lambda: roles.parse_card_content(text, None),
        )
        template = roles.extract_template(text)
        sides = roles.extract_sides(text)  # 양면/단면 → 뒷면 생성 여부

        if view.design_mode:
            # 이미 시안 모드: 내용 추가·수정, 템플릿 변경, 인쇄면 변경일 때만 재생성.
            # 수량·확정 같은 메시지는 일반 흐름(슬롯 파싱)으로 흘려보낸다.
            if not content.filled_fields() and not template and not sides:
                return None
        elif not (design_ask or content.is_generatable()):
            # 시안 모드 진입 조건: 시안 제작 요청(C) 또는 생성 가능한 내용
            return None

        result = self.service.handle_card_content(session_id, content, template=template, sides=sides)
        return result, self._render(result, adapter)

    def process_design(
        self,
        session_id: str,
        template: str | None = None,
        fields: dict | None = None,
        sides: str | None = None,
    ) -> tuple[TurnResult, str]:
        """UI에서 템플릿 변경·내용 수정·인쇄면 변경 → 시안 재생성."""
        content = CardContent(**(fields or {}))
        result = self.service.handle_card_content(session_id, content, template=template, sides=sides)
        return result, self._render(result, self.adapter_provider())

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

    def process_select(self, session_id: str, slot: str, value) -> tuple[TurnResult, str]:
        """질문 옵션 버튼 클릭 → 슬롯 직접 설정."""
        result = self.service.select_option(session_id, slot, value)
        return result, self._render(result, self.adapter_provider())

    def process_reopen(self, session_id: str, slot: str) -> tuple[TurnResult, str]:
        """최종 확인에서 특정 항목 '바꾸기' → 그 슬롯을 다시 고르게 띄운다."""
        result = self.service.reopen_slot(session_id, slot)
        return result, self._render(result, self.adapter_provider())

    def process_cutline(
        self, session_id: str, saved_path: str | Path, original_name: str = ""
    ) -> tuple[TurnResult, str]:
        """도무송 칼선 파일 접수 → 검증 → 재검판."""
        result = self.service.handle_cutline(session_id, saved_path, original_name)
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
            except Exception:
                # API 오류(요청 오류·레이트리밋·네트워크 등)에도 대화가 끊기지 않게 규칙으로 폴백
                self.service.store.record_llm_parse_failure(session_id)
                break
            self.service.store.reset_llm_parse_failures(session_id)
            return proposal
        return rule()  # 실패 → 규칙 폴백으로 계속 진행 (카운트는 시그널로 남음)

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
