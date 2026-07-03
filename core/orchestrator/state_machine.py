"""주문 세션 상태머신 — 상태 정의와 전이 규칙.

철칙: 상태를 바꾸는 코드는 오케스트레이터뿐이다. LLM 출력은 제안일 뿐이며
여기 정의된 전이 테이블을 통과하지 못하는 변경은 존재할 수 없다.
모든 전이는 이벤트 로그(session.py)에 남는다.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    INTAKE = "INTAKE"                # 세션 시작, 첫 발화/파일 대기
    CLASSIFY = "CLASSIFY"            # 고객 유형(A/B/C)·상품 인식
    SLOT_FILLING = "SLOT_FILLING"    # 주문 사양 수집 (질문 정책 적용)
    FILE_CHECK = "FILE_CHECK"        # 프리플라이트 실행·결과 반영
    PROOF_CONFIRM = "PROOF_CONFIRM"  # 최종 사양·검판 결과·견적 고객 확정
    PAYMENT_MOCK = "PAYMENT_MOCK"    # 결제 목업 (프로토타입)
    COMPLETED = "COMPLETED"          # 주문 확정 완료
    ESCALATED = "ESCALATED"          # 사람 검판 큐로 이동 (프로토타입은 로그+카드)


#: 허용 전이 테이블. 여기 없는 전이는 TransitionError.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.INTAKE: frozenset({State.CLASSIFY, State.ESCALATED}),
    State.CLASSIFY: frozenset({State.SLOT_FILLING, State.ESCALATED}),
    State.SLOT_FILLING: frozenset({State.FILE_CHECK, State.PROOF_CONFIRM, State.ESCALATED}),
    State.FILE_CHECK: frozenset({State.SLOT_FILLING, State.PROOF_CONFIRM, State.ESCALATED}),
    # PROOF_CONFIRM에서 고객이 사양을 바꾸면 SLOT_FILLING, 파일을 다시 올리면 FILE_CHECK로 복귀
    State.PROOF_CONFIRM: frozenset(
        {State.SLOT_FILLING, State.FILE_CHECK, State.PAYMENT_MOCK, State.ESCALATED}
    ),
    State.PAYMENT_MOCK: frozenset({State.COMPLETED, State.ESCALATED}),
    State.COMPLETED: frozenset(),
    # 에스컬레이션은 사람이 해소하면 슬롯 수집으로 복귀 (본개발: 검판자 큐 UI)
    State.ESCALATED: frozenset({State.SLOT_FILLING, State.FILE_CHECK}),
}

INITIAL_STATE = State.INTAKE

#: 어느 상태에서든 진입 가능한 상태 (전이 테이블에 이미 반영되어 있으나 명시)
ALWAYS_REACHABLE = frozenset({State.ESCALATED})


class TransitionError(Exception):
    """허용되지 않은 상태 전이 시도."""

    def __init__(self, current: State, target: State):
        self.current = current
        self.target = target
        super().__init__(f"허용되지 않은 전이: {current} → {target}")


def validate_transition(current: State, target: State) -> None:
    """전이가 허용되는지 검증. 아니면 TransitionError."""
    if target not in TRANSITIONS[current]:
        raise TransitionError(current, target)


def can_transition(current: State, target: State) -> bool:
    return target in TRANSITIONS[current]
