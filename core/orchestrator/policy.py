"""질문 정책 + 에스컬레이션 시그널 + 3중 관문 — 전부 순수 함수 (DB·LLM 무관).

PLAN §2 질문 정책: required ∧ (값 없음 ∧ 추론 실패) ∧ (기본값 없음 ∨ 위험 높음) 일 때만 질문.
PLAN §8 에스컬레이션 시그널 표를 그대로 상수화.
PLAN §7 gates: 생산 진입 = preflight_all_pass ∧ customer_confirmed ∧ no_escalation.

이 모듈은 상태를 읽지도 쓰지도 않는다. 입력(스키마·슬롯·추론값·리포트)을 받아
결정(PolicyDecision/GateResult)만 반환하고, 적용은 오케스트레이터가 한다.
LLM은 여기서 나온 질문 목록을 자연어로 '번역'만 한다 (철칙 1·2).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.preflight.report import PreflightReport
from core.products.schema import ProductSchema, Risk

# ---------------------------------------------------------------- 시그널 상수 (PLAN §8)

#: 고객-AI 왕복 수 임계 (초과 시 시그널)
MAX_TURNS = 6
#: 같은 슬롯 값 변경 횟수 임계 (이상 시 시그널)
SLOT_CHANGE_LIMIT = 2
#: 주문 예상 금액 임계, KRW (초과 시 시그널 — 임의 초기값)
QUOTE_ESCALATION_KRW = 300_000
#: LLM 출력 스키마 검증 연속 실패 임계 (이상 시 시그널)
PARSE_FAILURE_LIMIT = 2

SIG_TURNS_EXCEEDED = "turns_exceeded"            # 왕복 > 6회
SIG_SLOT_THRASHING = "slot_thrashing"            # 같은 슬롯 2회 이상 변경 (":슬롯명" 접미)
SIG_PREFLIGHT_UNCERTAIN = "preflight_uncertain"  # uncertain 항목 존재 (":체크id" 접미)
SIG_HIGH_QUOTE = "quote_over_threshold"          # 금액 > 30만원
SIG_PARSE_FAILURES = "llm_parse_failures"        # 스키마 검증 실패 2회 연속
SIG_NEGATIVE_SENTIMENT = "negative_sentiment"    # 부정 감정 표현 감지

#: uncertain이어도 사람 검판이 아니라 '고객 질문'으로 해소되는 항목 (에스컬레이션 제외).
#: dieline(칼선 없음)은 재단 형태(사각/도무송)를 고객이 정하면 판정이 확정된다.
CUSTOMER_RESOLVABLE_UNCERTAIN = frozenset({"dieline"})

# ---------------------------------------------------------------- 관문 차단 사유 상수

BLOCK_PREFLIGHT_MISSING = "preflight_not_run"          # 리포트 자체가 없음
BLOCK_CUSTOMER_NOT_CONFIRMED = "customer_not_confirmed"
BLOCK_ESCALATED = "escalated"

# ---------------------------------------------------------------- 결정 모델


class SlotQuestion(BaseModel):
    """고객에게 물어야 하는 슬롯 1개. LLM이 이걸 자연어 질문으로 번역한다.

    options: 클릭으로 고를 수 있는 값 목록(원값). UI가 라벨을 붙여 버튼으로 보여준다.
    allow_other: 목록에 없으면 '기타(직접 입력)'로 자유 입력을 허용.
    """

    slot: str
    display_name: str = ""
    reason: str = ""                                   # 질문이 발생한 규칙 (감사·eval용)
    quick_options: list[Any] = Field(default_factory=list)
    options: list[Any] = Field(default_factory=list)   # 선택 버튼용 값 목록 (quick_options ∨ choices)
    allow_other: bool = True                            # '기타' 직접 입력 허용


class AutoFill(BaseModel):
    """질문 없이 기본값으로 채운 슬롯 — 고객에게는 '통보'만 한다."""

    slot: str
    value: Any = None
    note: str = ""


class Conflict(BaseModel):
    """파일 추론값과 고객 발화값이 다른 슬롯 — 확인 질문 대상 (ask_if_conflict)."""

    slot: str
    display_name: str = ""
    user_value: Any = None
    inferred_value: Any = None


class PolicyDecision(BaseModel):
    questions: list[SlotQuestion] = Field(default_factory=list)
    auto_filled: list[AutoFill] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


class GateResult(BaseModel):
    ok: bool
    blockers: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 질문 정책


def _slot_value(slots: dict[str, dict[str, Any]], name: str) -> Any:
    """현재 채워진 슬롯 값 (없거나 None이면 '값 없음')."""
    entry = slots.get(name)
    if not isinstance(entry, dict):
        return None
    return entry.get("value")


def next_actions(
    schema: ProductSchema,
    slots: dict[str, dict[str, Any]],
    inferred: dict[str, Any],
    report: PreflightReport | None = None,
) -> PolicyDecision:
    """스키마 선언 순서대로 슬롯을 훑어 질문/자동채움/충돌을 결정한다.

    입력:
      slots    — 세션에 이미 기록된 값 {슬롯: {value, source, ...}}
      inferred — 파일 측정에서 추론된 값 {슬롯: 값} (측정→추론 변환은 오케스트레이터 몫.
                 예: file_trimbox → size, dieline_present → cut_type)
      report   — 프리플라이트 리포트. 현재 질문 생성에는 쓰지 않는다. uncertain 처리는
                 escalation_signals가 담당하며, 이 인자는 향후 uncertain 기반 확인 질문
                 (trim_safety/dieline 의도 확인) 확장을 위한 계약 유지용.

    질문 규칙 (PLAN §2):
      required ∧ (값 없음 ∧ 추론 실패) ∧ (기본값 없음 ∨ risk_if_defaulted == high)
    자동 채움 규칙:
      값 없음 ∧ 추론 실패 ∧ 기본값 있음 ∧ risk != high → 질문 없이 채우고 통보.
      (risk medium은 PLAN상 '기본값 제안 + 가벼운 확인' — 질문은 아니므로 auto_filled에
       넣되 note로 구분한다. 확인 문장 생성은 LLM 몫.)
    """
    decision = PolicyDecision()

    for name, sdef in schema.slots.items():  # dict 순서 = 스키마 선언 순서 = 질문 순서
        display = sdef.display_name or name
        user_value = _slot_value(slots, name)
        has_value = user_value is not None
        inferred_value = inferred.get(name)
        has_inferred = inferred_value is not None

        # 1) 충돌: 이미 값이 있는데 추론값과 다르고 ask_if_conflict면 확인 대상
        if has_value and has_inferred and user_value != inferred_value and sdef.ask_if_conflict:
            decision.conflicts.append(
                Conflict(
                    slot=name,
                    display_name=display,
                    user_value=user_value,
                    inferred_value=inferred_value,
                )
            )

        # 2) 값이 있거나 추론에 성공했으면 질문하지 않는다
        if has_value or has_inferred:
            continue

        # 3) 값 없음 ∧ 추론 실패 — 기본값으로 해소 가능한가?
        if sdef.has_default and sdef.risk_if_defaulted != Risk.HIGH:
            if sdef.risk_if_defaulted == Risk.LOW:
                note = f"기본값 '{sdef.default}' 적용 (위험도 low — 통보 후 진행)"
            else:  # medium
                note = f"기본값 '{sdef.default}' 제안 (위험도 medium — 가벼운 확인 권장)"
            decision.auto_filled.append(AutoFill(slot=name, value=sdef.default, note=note))
            continue

        # 4) 남은 경우: 기본값 없음 ∨ 위험 높음 → required일 때만 질문
        if sdef.required:
            if not sdef.has_default:
                reason = "required_no_default"
            else:
                reason = "required_default_high_risk"  # 틀리면 실물 파손 → 반드시 확정
            # 선택 버튼용 옵션: quick_options 우선, 없으면 choices. 항상 '기타' 직접 입력 허용.
            opts = list(sdef.quick_options) or list(sdef.choices)
            decision.questions.append(
                SlotQuestion(
                    slot=name,
                    display_name=display,
                    reason=reason,
                    quick_options=list(sdef.quick_options),
                    options=opts,
                    allow_other=True,
                )
            )
        # required 아니고 기본값도 없으면: 아무것도 하지 않는다 (선택 사양)

    return decision


# ---------------------------------------------------------------- 에스컬레이션 시그널 (PLAN §8)


def escalation_signals(
    turn_count: int,
    slot_change_counts: dict[str, int],
    report: PreflightReport | None,
    quote_total: float | int | None,
    llm_parse_failures: int,
    negative_sentiment: bool,
) -> list[str]:
    """PLAN §8 표 그대로. 시그널이 하나라도 있으면 오케스트레이터가 escalate()를 호출한다.

    반환은 시그널 코드 문자열 목록. 슬롯/체크 관련 시그널은 ':이름'을 붙여
    어떤 항목이 원인인지 로그에서 바로 보이게 한다.
    """
    signals: list[str] = []

    # 대화가 길어지거나(왕복 많음) 사양을 여러 번 바꾸는 것은 '정상 대화'다 — 사람에게 넘기지 않는다.
    # (예전엔 왕복>6, 같은 슬롯 2회 변경 시 에스컬레이션했으나, 대화를 끊어 챗봇처럼 느껴져 제거.)
    # 사양 변경·대화 길이는 마찰이 아니라 상담의 일부다. 필요한 신호만 아래에서 본다.

    # 프리플라이트 uncertain 항목 존재 — 즉시.
    # 단, 고객 질문으로 풀리는 항목(dieline=재단 형태 선택)은 사람에게 넘기지 않는다.
    if report is not None:
        for r in report.uncertains:
            if r.check_id in CUSTOMER_RESOLVABLE_UNCERTAIN:
                continue
            signals.append(f"{SIG_PREFLIGHT_UNCERTAIN}:{r.check_id}")

    # 주문 예상 금액 > 30만원 (임의 초기값)
    if quote_total is not None and quote_total > QUOTE_ESCALATION_KRW:
        signals.append(SIG_HIGH_QUOTE)

    # LLM 출력 스키마 검증 실패 2회 연속
    if llm_parse_failures >= PARSE_FAILURE_LIMIT:
        signals.append(SIG_PARSE_FAILURES)

    # 고객 부정 감정 표현 감지 — 즉시
    if negative_sentiment:
        signals.append(SIG_NEGATIVE_SENTIMENT)

    return signals


# ---------------------------------------------------------------- 3중 관문 (PLAN §7 gates)


def production_gate(
    report: PreflightReport | None,
    customer_confirmed: bool,
    escalated: bool,
) -> GateResult:
    """생산 진입 3중 관문: preflight 통과 ∧ 고객 확정 ∧ 에스컬레이션 없음.

    report.gate_ok는 fail 0건 그리고 uncertain 0건을 뜻한다 (report.py 참조) —
    uncertain은 해소 전까지 차단이다. 셋 중 하나라도 어긋나면 ok=False,
    blockers에 사유가 전부 담긴다 (관문 우회 = 치명 지표, PLAN §9).
    """
    blockers: list[str] = []

    if report is None:
        blockers.append(BLOCK_PREFLIGHT_MISSING)
    elif not report.gate_ok:
        for r in report.failures:
            blockers.append(f"preflight_fail:{r.check_id}")
        for r in report.uncertains:
            blockers.append(f"preflight_uncertain:{r.check_id}")

    if not customer_confirmed:
        blockers.append(BLOCK_CUSTOMER_NOT_CONFIRMED)

    if escalated:
        blockers.append(BLOCK_ESCALATED)

    return GateResult(ok=not blockers, blockers=blockers)
