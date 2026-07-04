"""dialog eval 하네스 테스트 — 시나리오 구성·채점기·M4 DoD 봉인 (PLAN §5 M4, §9).

- 시나리오는 정확히 15개, A/B/엣지 5개씩 (id 접두사 규약 a*/b*/e*)
- 채점기(관문 우회 탐지, 슬롯 대조)는 합성 입력으로 단위 검증
- 규칙 폴백(adapter=None) 전체 실행이 목표(통과 ≥13, A평균 질문 ≤3, 치명 0)를 달성
"""

from __future__ import annotations

import json

from evals.run_dialog_eval import (
    TARGET_A_AVG_QUESTIONS,
    TARGET_PASS_COUNT,
    gate_bypassed,
    grade,
    load_scenarios,
    run_all,
    scenario_type,
)

# ---------------------------------------------------------------- 시나리오 구성


def test_scenarios_are_16_with_5_5_6_mix():
    """A유형 5, B유형 5, 엣지 6 (명함 시안 생성 해피패스 e6 추가)."""
    scenarios = load_scenarios()
    assert len(scenarios) == 16
    ids = [s["id"] for s in scenarios]
    assert len(set(ids)) == 16, "시나리오 id 중복"
    by_type = [scenario_type(i) for i in ids]
    assert by_type.count("A") == 5
    assert by_type.count("B") == 5
    assert by_type.count("EDGE") == 6


def test_scenario_steps_use_known_step_kinds():
    for sc in load_scenarios():
        assert sc["steps"], f"{sc['id']}: steps 비어 있음"
        for step in sc["steps"]:
            kind = next(iter(step))
            assert kind in ("user", "upload", "action"), f"{sc['id']}: 알 수 없는 스텝 {step}"
            if kind == "action":
                action = str(step["action"])
                assert action == "confirm" or action.startswith("autofix:")


def test_b_type_includes_real_bleed_corpus_and_autofix():
    """B유형 계약: bleed 결함은 실제 코퍼스 파일 + autofix 흐름 포함 (PLAN §5 M4)."""
    b_scenarios = [s for s in load_scenarios() if scenario_type(s["id"]) == "B"]
    has_bleed_autofix = any(
        any("bleed" in str(st.get("upload", "")) for st in s["steps"])
        and any(str(st.get("action", "")) == "autofix:bleed" for st in s["steps"])
        for s in b_scenarios
    )
    assert has_bleed_autofix


# ---------------------------------------------------------------- 채점기 단위 검증


def _ev(seq: int, type_: str, payload: dict) -> dict:
    return {"seq": seq, "ts": "", "type": type_, "payload": payload}


def test_gate_bypass_detector_flags_missing_gate_check():
    events = [
        _ev(1, "session_created", {}),
        _ev(2, "transition", {"from": "PAYMENT_MOCK", "to": "COMPLETED"}),
    ]
    assert gate_bypassed(events)


def test_gate_bypass_detector_accepts_gated_completion():
    events = [
        _ev(1, "gate_check", {"ok": True, "blockers": []}),
        _ev(2, "transition", {"from": "PAYMENT_MOCK", "to": "COMPLETED"}),
    ]
    assert not gate_bypassed(events)
    # gate_check가 전이보다 '뒤'(seq 큰 쪽)에만 있으면 우회다 (선행 조건)
    late_gate = [
        _ev(1, "transition", {"from": "PAYMENT_MOCK", "to": "COMPLETED"}),
        _ev(2, "gate_check", {"ok": True, "blockers": []}),
    ]
    assert gate_bypassed(late_gate)


def test_gate_bypass_detector_ignores_failed_gate_without_completion():
    events = [_ev(1, "gate_check", {"ok": False, "blockers": ["escalated"]})]
    assert not gate_bypassed(events)


class _FakeView:
    def __init__(self, state="COMPLETED", slots=None, escalated=False):
        self.state = state
        self.slots = slots or {}
        self.escalated = escalated


def test_grade_marks_wrong_slot_as_critical():
    scenario = {
        "id": "a9_fake",
        "description": "채점기 검증용",
        "expect": {"completed": True, "final_state": "COMPLETED", "slots": {"quantity": 500}},
    }
    view = _FakeView(slots={"quantity": {"value": 1000}})
    events = [
        _ev(1, "gate_check", {"ok": True}),
        _ev(2, "transition", {"to": "COMPLETED"}),
    ]
    out = grade(scenario, view, events, questions=0)
    assert not out.passed
    assert any("quantity" in c for c in out.criticals)


def test_grade_passes_exact_match():
    scenario = {
        "id": "a9_fake",
        "description": "채점기 검증용",
        "expect": {
            "completed": True,
            "final_state": "COMPLETED",
            "max_questions": 1,
            "slots": {"size": "90x90"},
            "escalated": False,
        },
    }
    view = _FakeView(slots={"size": {"value": "90x90"}})
    events = [
        _ev(1, "gate_check", {"ok": True}),
        _ev(2, "transition", {"to": "COMPLETED"}),
    ]
    out = grade(scenario, view, events, questions=1)
    assert out.passed, (out.failures, out.criticals)


def test_grade_checks_escalation_reason_substring():
    scenario = {
        "id": "e9_fake",
        "description": "채점기 검증용",
        "expect": {
            "completed": False,
            "final_state": "SLOT_FILLING",
            "escalated": True,
            "escalation_contains": "slot_thrashing",
        },
    }
    view = _FakeView(state="SLOT_FILLING", escalated=True)
    events = [_ev(1, "escalated", {"reason": "quote_over_threshold"})]
    out = grade(scenario, view, events, questions=0)
    assert any("slot_thrashing" in f for f in out.failures)


# ---------------------------------------------------------------- M4 DoD 봉인 (전체 실행)


def test_rule_mode_meets_m4_goals(tmp_path):
    """규칙 폴백으로 15개 시나리오 전체 실행 — 통과 ≥13, A평균 질문 ≤3, 치명 0."""
    report_path = tmp_path / "dialog_eval.json"
    outcomes, summary = run_all(report_path=report_path, mode="rule")

    failed = [(o.id, o.criticals + o.failures) for o in outcomes if not o.passed]
    assert summary["passed"] >= TARGET_PASS_COUNT, f"통과 미달: {failed}"
    assert summary["a_type_avg_questions"] <= TARGET_A_AVG_QUESTIONS
    assert summary["critical_count"] == 0, f"치명 위반: {failed}"
    assert summary["goal_met"]

    # 리포트 파일이 계약 형태로 저장된다
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 16
    assert len(data["scenarios"]) == 16
    assert {"id", "passed", "questions", "criticals"} <= set(data["scenarios"][0])
