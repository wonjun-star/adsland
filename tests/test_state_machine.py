import pytest

from core.orchestrator.state_machine import (
    TRANSITIONS,
    State,
    TransitionError,
    can_transition,
    validate_transition,
)


def test_happy_path_full_journey():
    path = [
        State.INTAKE,
        State.CLASSIFY,
        State.SLOT_FILLING,
        State.FILE_CHECK,
        State.PROOF_CONFIRM,
        State.PAYMENT_MOCK,
        State.COMPLETED,
    ]
    for cur, nxt in zip(path, path[1:]):
        validate_transition(cur, nxt)  # 예외 없어야 함


def test_file_check_loops_back_to_slot_filling():
    validate_transition(State.FILE_CHECK, State.SLOT_FILLING)
    validate_transition(State.SLOT_FILLING, State.FILE_CHECK)


def test_illegal_jump_intake_to_payment():
    with pytest.raises(TransitionError):
        validate_transition(State.INTAKE, State.PAYMENT_MOCK)


def test_completed_is_terminal():
    assert TRANSITIONS[State.COMPLETED] == frozenset()


def test_every_active_state_can_escalate():
    for state in State:
        if state in (State.COMPLETED, State.ESCALATED):
            continue
        assert can_transition(state, State.ESCALATED), state


def test_proof_confirm_can_return_to_slot_filling():
    validate_transition(State.PROOF_CONFIRM, State.SLOT_FILLING)
