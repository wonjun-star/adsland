"""대화 시나리오 자동 채점 하네스 (PLAN §5 M4, §9).

evals/scenarios/*.yaml 의 시나리오를 ChatPipeline에 그대로 재생하고 결정론적으로 채점한다.
기본은 adapter=None(규칙 폴백) — API 키 없이 항상 같은 결과가 재현돼야 한다.
--llm 플래그를 주면 같은 시나리오를 실제 LLM으로 돌린다 (ANTHROPIC_API_KEY 필요).

시나리오 형식 (yaml):
    id: a1_...            # 접두사로 유형 구분: a*=A유형, b*=B유형, e*=엣지
    description: ...
    steps:                # 순서대로 재생
      - user: "발화"                       # process_message
      - upload: "data/samples/..."         # process_upload (프로젝트 루트 기준 경로)
      - action: confirm | "autofix:bleed"  # process_confirm / process_autofix
    expect:
      completed: bool          # 최종 상태가 COMPLETED인가
      final_state: 상태명
      max_questions: int       # 턴별 directives.questions 합의 상한 (없으면 미검사)
      slots: {슬롯: 값}        # 최종 세션 슬롯과 대조 — 불일치는 치명
      escalated: bool
      escalation_contains: 부분문자열   # (선택) 에스컬레이션 사유에 포함돼야 함

채점 항목 (PLAN §9):
    완주(final_state 일치) / 질문 수 / 잘못된 슬롯 확정(치명) / 관문 우회(치명) / 에스컬레이션 일치.
    관문 우회 = 이벤트 로그에서 COMPLETED 전이 앞에 ok=true인 gate_check 이벤트가 없는 것.

목표 (미달 시 exit 1): 통과 ≥13/15, A유형 평균 질문 ≤3, 치명 위반 0.
실행: python -m evals.run_dialog_eval [--llm]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from core.llm.adapter import LLMAdapter, get_adapter
from core.orchestrator.chat import ChatPipeline
from core.orchestrator.service import IntakeService
from core.orchestrator.session import PROJECT_ROOT, SessionStore

SCENARIO_DIR = Path(__file__).parent / "scenarios"
REPORT_PATH = Path(__file__).parent / "reports" / "dialog_eval.json"

#: 목표치 (PLAN §5 M4 DoD, §9)
#: A유형 평균 질문: 사양(사이즈·용지·코팅·재단·수량)을 버튼으로 다 물어보는 설계라
#: 예전 '최소 질문(≤3)'보다 높다 — 대신 '추천대로' 한 번에 넘어갈 수 있어 마찰은 낮다.
TARGET_PASS_COUNT = 13
TARGET_A_AVG_QUESTIONS = 6.0

#: id 접두사 → 유형 라벨 (a1_* / b3_* / e5_* 규약)
_TYPE_RE = re.compile(r"^([abe])\d+_", re.IGNORECASE)
_TYPE_LABELS = {"a": "A", "b": "B", "e": "EDGE"}


def scenario_type(scenario_id: str) -> str:
    m = _TYPE_RE.match(scenario_id)
    return _TYPE_LABELS.get(m.group(1).lower(), "?") if m else "?"


# ---------------------------------------------------------------- 시나리오 로딩


def load_scenarios(scenario_dir: str | Path = SCENARIO_DIR) -> list[dict]:
    """scenarios/*.yaml 전부 로드 (파일명 정렬 = 실행 순서). 필수 키를 즉시 검증한다."""
    scenarios: list[dict] = []
    for path in sorted(Path(scenario_dir).glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for key in ("id", "description", "steps", "expect"):
            if key not in data:
                raise ValueError(f"{path.name}: 필수 키 없음: {key}")
        scenarios.append(data)
    return scenarios


# ---------------------------------------------------------------- 실행·채점 결과


@dataclass
class Outcome:
    """시나리오 1건의 실행+채점 결과."""

    id: str
    type: str
    description: str
    final_state: str = ""
    expected_state: str = ""
    questions: int = 0
    max_questions: int | None = None
    escalated: bool = False
    escalation_reasons: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # 비치명 불일치
    criticals: list[str] = field(default_factory=list)  # 치명: 잘못된 슬롯 확정, 관문 우회

    @property
    def passed(self) -> bool:
        return not self.failures and not self.criticals

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "passed": self.passed,
            "final_state": self.final_state,
            "expected_state": self.expected_state,
            "questions": self.questions,
            "max_questions": self.max_questions,
            "escalated": self.escalated,
            "escalation_reasons": self.escalation_reasons,
            "failures": self.failures,
            "criticals": self.criticals,
        }


# ---------------------------------------------------------------- 시나리오 실행


def _play_step(pipe: ChatPipeline, session_id: str, step: dict) -> Any:
    """스텝 1개 재생. 반환은 TurnResult."""
    if "user" in step:
        result, _reply = pipe.process_message(session_id, str(step["user"]))
        return result
    if "upload" in step:
        path = PROJECT_ROOT / str(step["upload"])
        if not path.exists():
            raise FileNotFoundError(f"샘플 파일 없음 (make gen-samples 필요): {path}")
        result, _reply = pipe.process_upload(session_id, path, path.name)
        return result
    if "action" in step:
        action = str(step["action"])
        if action == "confirm":
            result, _reply = pipe.process_confirm(session_id)
            return result
        if action.startswith("autofix:"):
            result, _reply = pipe.process_autofix(session_id, action.split(":", 1)[1])
            return result
        raise ValueError(f"지원하지 않는 action: {action}")
    raise ValueError(f"알 수 없는 스텝: {step}")


def gate_bypassed(events: list[dict]) -> bool:
    """관문 우회 탐지: COMPLETED 전이가 있는데 그보다 앞선(ok=true) gate_check가 없으면 우회.

    events는 IntakeService.transcript() 형식: {seq, ts, type, payload}.
    COMPLETED 전이가 아예 없으면 우회도 없다 (False).
    """
    completions = [
        e["seq"]
        for e in events
        if e["type"] == "transition" and (e.get("payload") or {}).get("to") == "COMPLETED"
    ]
    for done_seq in completions:
        ok_before = any(
            e["type"] == "gate_check" and (e.get("payload") or {}).get("ok") is True and e["seq"] < done_seq
            for e in events
        )
        if not ok_before:
            return True
    return False


def _values_equal(expected: Any, actual: Any) -> bool:
    """슬롯 값 비교 — yaml/int/str 표기 차이는 흡수하되 의미가 다르면 불일치."""
    return expected == actual or str(expected) == str(actual)


def grade(scenario: dict, view: Any, events: list[dict], questions: int) -> Outcome:
    """실행 결과(view=SessionView, events=이벤트 로그) → 채점."""
    expect: dict = scenario["expect"]
    out = Outcome(
        id=scenario["id"],
        type=scenario_type(scenario["id"]),
        description=scenario["description"],
        final_state=view.state,
        expected_state=str(expect.get("final_state", "")),
        questions=questions,
        max_questions=expect.get("max_questions"),
        escalated=view.escalated,
        escalation_reasons=[
            str((e.get("payload") or {}).get("reason", "")) for e in events if e["type"] == "escalated"
        ],
    )

    # 1) 완주 여부: 최종 상태 일치 (+ completed 불리언 정합)
    if out.final_state != out.expected_state:
        out.failures.append(f"최종 상태 불일치: 기대 {out.expected_state}, 실제 {out.final_state}")
    expected_completed = bool(expect.get("completed", False))
    if (out.final_state == "COMPLETED") != expected_completed:
        out.failures.append(f"완주 여부 불일치: 기대 completed={expected_completed}")

    # 2) 질문 수: 턴별 directives.questions 합 ≤ max_questions
    if out.max_questions is not None and questions > int(out.max_questions):
        out.failures.append(f"질문 수 초과: {questions} > 허용 {out.max_questions}")

    # 3) 잘못된 슬롯 확정 (치명): 기대 슬롯 값과 최종 세션 슬롯 값 대조
    for name, want in (expect.get("slots") or {}).items():
        got = ((view.slots or {}).get(name) or {}).get("value")
        if not _values_equal(want, got):
            out.criticals.append(f"슬롯 확정 오류: {name} 기대 {want!r}, 실제 {got!r}")

    # 4) 관문 우회 (치명): gate_check ok=true 없이 COMPLETED 전이
    if gate_bypassed(events):
        out.criticals.append("관문 우회: gate_check(ok=true) 없이 COMPLETED 전이")

    # 5) 에스컬레이션 일치
    expected_escalated = bool(expect.get("escalated", False))
    if view.escalated != expected_escalated:
        out.failures.append(f"에스컬레이션 불일치: 기대 {expected_escalated}, 실제 {view.escalated}")
    needle = expect.get("escalation_contains")
    if needle and not any(needle in r for r in out.escalation_reasons):
        out.failures.append(f"에스컬레이션 사유에 '{needle}' 없음: {out.escalation_reasons}")

    return out


def run_scenario(
    scenario: dict,
    adapter_provider: Callable[[], LLMAdapter | None],
) -> Outcome:
    """시나리오 1건: 새 인메모리 세션으로 스텝을 재생하고 채점한다."""
    service = IntakeService(store=SessionStore("sqlite:///:memory:"))
    pipe = ChatPipeline(service, adapter_provider=adapter_provider)
    start_result, _greeting = pipe.start()
    session_id = start_result.session.id

    questions = 0
    for step in scenario["steps"]:
        result = _play_step(pipe, session_id, step)
        questions += len(result.directives.questions)

    view = service.view_session(session_id)
    events = service.transcript(session_id)
    return grade(scenario, view, events, questions)


# ---------------------------------------------------------------- 합산·리포트


def summarize(outcomes: list[Outcome], mode: str) -> dict:
    """시나리오 결과 → 합계 지표 (PLAN §9 표의 dialog eval 항목)."""
    a_outcomes = [o for o in outcomes if o.type == "A"]
    a_avg = (sum(o.questions for o in a_outcomes) / len(a_outcomes)) if a_outcomes else 0.0
    passed = sum(1 for o in outcomes if o.passed)
    critical_count = sum(len(o.criticals) for o in outcomes)
    return {
        "run_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "total": len(outcomes),
        "passed": passed,
        "a_type_avg_questions": round(a_avg, 2),
        "critical_count": critical_count,
        "targets": {
            "passed_min": TARGET_PASS_COUNT,
            "a_type_avg_questions_max": TARGET_A_AVG_QUESTIONS,
            "critical_max": 0,
        },
        "goal_met": (passed >= TARGET_PASS_COUNT and a_avg <= TARGET_A_AVG_QUESTIONS and critical_count == 0),
    }


def run_all(
    scenario_dir: str | Path = SCENARIO_DIR,
    adapter_provider: Callable[[], LLMAdapter | None] | None = None,
    report_path: str | Path | None = REPORT_PATH,
    mode: str = "rule",
) -> tuple[list[Outcome], dict]:
    """전체 시나리오 실행 + 합산 + (지정 시) JSON 리포트 저장. 테스트에서도 그대로 쓴다."""
    provider = adapter_provider or (lambda: None)
    outcomes = [run_scenario(sc, provider) for sc in load_scenarios(scenario_dir)]
    summary = summarize(outcomes, mode)

    if report_path is not None:
        report = {"summary": summary, "scenarios": [o.to_json() for o in outcomes]}
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return outcomes, summary


# ---------------------------------------------------------------- 출력


def _print_table(outcomes: list[Outcome], summary: dict, report_path: Path | None) -> None:
    # Windows 콘솔(cp949 등)에서 인코딩 불가 문자로 죽지 않게 — 리포트 JSON이 원본이다
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    header = f"{'시나리오':<34} {'유형':<5} {'질문':>4} {'최종 상태':<14} {'결과':<6} 비고"
    print("=" * 96)
    print(f"대화 시나리오 채점 | 모드: {summary['mode']}")
    print("=" * 96)
    print(header)
    print("-" * 96)
    for o in outcomes:
        problems = o.criticals + o.failures
        note = "; ".join(problems) if problems else "-"
        verdict = "PASS" if o.passed else ("치명" if o.criticals else "FAIL")
        print(f"{o.id:<34} {o.type:<5} {o.questions:>4} {o.final_state:<14} {verdict:<6} {note}")
    print("-" * 96)
    print(
        f"통과 {summary['passed']}/{summary['total']} (목표 >={TARGET_PASS_COUNT})  |  "
        f"A유형 평균 질문 {summary['a_type_avg_questions']} (목표 <={TARGET_A_AVG_QUESTIONS:g})  |  "
        f"치명 위반 {summary['critical_count']} (목표 0)"
    )
    print(f"종합: {'목표 달성' if summary['goal_met'] else '목표 미달'}")
    if report_path is not None:
        print(f"리포트 저장: {report_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="대화 시나리오 자동 채점 (M4)")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="실제 LLM 모드로 실행 (ANTHROPIC_API_KEY 필요). 기본은 규칙 폴백(결정론).",
    )
    parser.add_argument(
        "--report",
        default=str(REPORT_PATH),
        help=f"결과 JSON 저장 경로 (기본: {REPORT_PATH})",
    )
    args = parser.parse_args(argv)

    if args.llm:
        adapter = get_adapter()
        if adapter is None:
            print("--llm 모드에는 ANTHROPIC_API_KEY가 필요합니다.", file=sys.stderr)
            return 2
        provider: Callable[[], LLMAdapter | None] = lambda: adapter  # noqa: E731
        mode = "llm"
    else:
        provider = lambda: None  # noqa: E731 — 규칙 폴백 강제 (키가 있어도 결정론 유지)
        mode = "rule"

    report_path = Path(args.report)
    outcomes, summary = run_all(adapter_provider=provider, report_path=report_path, mode=mode)
    _print_table(outcomes, summary, report_path)
    return 0 if summary["goal_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
