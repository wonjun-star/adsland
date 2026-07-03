"""SessionStore(세션 상태 + 이벤트 로그) 유닛테스트 — 인메모리 SQLite."""

import uuid

import pytest

from core.orchestrator.session import ENV_DB_VAR, SessionStore, resolve_db_url
from core.orchestrator.state_machine import State, TransitionError


@pytest.fixture
def store() -> SessionStore:
    return SessionStore("sqlite:///:memory:")


# ---------------------------------------------------------------- 생성·기본값


def test_create_session_defaults(store):
    s = store.create_session()
    uuid.UUID(s.id)  # uuid 형식이어야 함
    assert s.state == State.INTAKE.value
    assert s.customer_type is None
    assert s.product is None
    assert s.slots == {}
    assert s.file_path is None
    assert s.escalated is False
    assert s.escalation_reasons == []
    assert s.customer_confirmed is False
    assert s.llm_parse_failures == 0
    assert s.turn_count == 0


def test_create_session_records_creation_event(store):
    s = store.create_session()
    events = store.events(s.id)
    assert [e.type for e in events] == ["session_created"]
    assert events[0].seq == 1


def test_get_missing_returns_none(store):
    assert store.get("no-such-id") is None


# ---------------------------------------------------------------- 상태 전이


def test_valid_transition_updates_state_and_logs_event(store):
    s = store.create_session()
    store.transition(s.id, State.CLASSIFY, reason="첫 발화 수신")
    row = store.get(s.id)
    assert row.state == State.CLASSIFY.value
    ev = [e for e in store.events(s.id) if e.type == "transition"]
    assert len(ev) == 1
    assert ev[0].payload == {"from": "INTAKE", "to": "CLASSIFY", "reason": "첫 발화 수신"}


def test_invalid_transition_raises_and_changes_nothing(store):
    """전이 불허 시 TransitionError — 상태도 이벤트도 무변화 (트랜잭션)."""
    s = store.create_session()
    events_before = len(store.events(s.id))
    with pytest.raises(TransitionError):
        store.transition(s.id, State.PAYMENT_MOCK, reason="불법 점프")
    row = store.get(s.id)
    assert row.state == State.INTAKE.value          # 상태 무변화
    assert len(store.events(s.id)) == events_before  # 이벤트 무변화


def test_full_journey_via_store(store):
    s = store.create_session()
    path = [
        State.CLASSIFY,
        State.SLOT_FILLING,
        State.FILE_CHECK,
        State.PROOF_CONFIRM,
        State.PAYMENT_MOCK,
        State.COMPLETED,
    ]
    for target in path:
        store.transition(s.id, target)
    assert store.get(s.id).state == State.COMPLETED.value
    transitions = [e for e in store.events(s.id) if e.type == "transition"]
    assert len(transitions) == len(path)


def test_transition_missing_session_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.transition("no-such-id", State.CLASSIFY)


# ---------------------------------------------------------------- 슬롯


def test_set_slot_new_slot_starts_at_zero_changes(store):
    s = store.create_session()
    store.set_slot(s.id, "size", "90x50", source="inferred")
    slots = store.get(s.id).slots
    assert slots["size"] == {
        "value": "90x50",
        "source": "inferred",
        "confirmed": False,
        "change_count": 0,
    }


def test_set_slot_value_change_increments_count(store):
    s = store.create_session()
    store.set_slot(s.id, "quantity", 100, source="user")
    store.set_slot(s.id, "quantity", 500, source="user")   # 변경 1
    store.set_slot(s.id, "quantity", 1000, source="user")  # 변경 2
    entry = store.get(s.id).slots["quantity"]
    assert entry["value"] == 1000
    assert entry["change_count"] == 2


def test_set_slot_same_value_does_not_increment(store):
    s = store.create_session()
    store.set_slot(s.id, "material", "art_250", source="default")
    store.set_slot(s.id, "material", "art_250", source="user")  # 같은 값 재설정
    entry = store.get(s.id).slots["material"]
    assert entry["change_count"] == 0
    assert entry["source"] == "user"  # 출처는 갱신됨


def test_set_slot_logs_slot_set_events(store):
    s = store.create_session()
    store.set_slot(s.id, "size", "90x50", source="user")
    store.set_slot(s.id, "size", "100x100", source="user")
    ev = [e for e in store.events(s.id) if e.type == "slot_set"]
    assert len(ev) == 2
    assert ev[-1].payload["slot"] == "size"
    assert ev[-1].payload["change_count"] == 1


# ---------------------------------------------------------------- 에스컬레이션·확정


def test_escalate_sets_flag_and_accumulates_reasons(store):
    s = store.create_session()
    store.escalate(s.id, "preflight_uncertain:dieline")
    store.escalate(s.id, "negative_sentiment")
    row = store.get(s.id)
    assert row.escalated is True
    assert row.escalation_reasons == ["preflight_uncertain:dieline", "negative_sentiment"]
    ev = [e for e in store.events(s.id) if e.type == "escalated"]
    assert len(ev) == 2


def test_confirm_sets_customer_confirmed(store):
    s = store.create_session()
    store.confirm(s.id)
    row = store.get(s.id)
    assert row.customer_confirmed is True
    assert any(e.type == "customer_confirmed" for e in store.events(s.id))


# ---------------------------------------------------------------- 이벤트·카운터


def test_record_event_seq_is_monotonic(store):
    s = store.create_session()
    store.record_event(s.id, "llm_call", {"model": "haiku", "tokens": 123})
    store.set_slot(s.id, "size", "90x50", source="user")
    store.record_event(s.id, "quote", {"total": 45000})
    seqs = [e.seq for e in store.events(s.id)]
    assert seqs == list(range(1, len(seqs) + 1))  # 1부터 빈틈없이 단조 증가


def test_counters(store):
    s = store.create_session()
    store.increment_turn(s.id)
    store.increment_turn(s.id)
    store.record_llm_parse_failure(s.id)
    row = store.get(s.id)
    assert row.turn_count == 2
    assert row.llm_parse_failures == 1
    store.reset_llm_parse_failures(s.id)
    assert store.get(s.id).llm_parse_failures == 0


def test_meta_setters(store):
    s = store.create_session()
    store.set_customer_type(s.id, "A")
    store.set_product(s.id, "sticker")
    store.set_file_path(s.id, "data/uploads/x.pdf")
    row = store.get(s.id)
    assert (row.customer_type, row.product, row.file_path) == ("A", "sticker", "data/uploads/x.pdf")


# ---------------------------------------------------------------- DB URL 결정


def test_resolve_db_url_explicit_wins(monkeypatch):
    monkeypatch.setenv(ENV_DB_VAR, "sqlite:///env.db")
    assert resolve_db_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_resolve_db_url_env_url_passthrough(monkeypatch):
    monkeypatch.setenv(ENV_DB_VAR, "sqlite:///somewhere/env.db")
    assert resolve_db_url() == "sqlite:///somewhere/env.db"


def test_resolve_db_url_env_plain_path_wrapped(monkeypatch, tmp_path):
    p = tmp_path / "s.db"
    monkeypatch.setenv(ENV_DB_VAR, str(p))
    assert resolve_db_url() == f"sqlite:///{p.as_posix()}"


def test_resolve_db_url_default(monkeypatch):
    monkeypatch.delenv(ENV_DB_VAR, raising=False)
    url = resolve_db_url()
    assert url.startswith("sqlite:///")
    assert url.endswith("data/sessions.db")


def test_store_on_file_db_persists(tmp_path):
    url = f"sqlite:///{(tmp_path / 'x' / 'sessions.db').as_posix()}"  # 디렉터리 자동 생성 확인
    store1 = SessionStore(url)
    s = store1.create_session()
    store2 = SessionStore(url)
    assert store2.get(s.id) is not None
