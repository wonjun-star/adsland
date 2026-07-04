"""세션 상태 저장소 + 이벤트 로그 (SQLAlchemy 2.0, SQLite).

의존 방향(중요): core/llm 계층은 이 모듈을 절대 임포트하지 않는다.
    llm → (구조화된 제안 dict 반환) → 오케스트레이터(이 모듈 사용) → DB
상태를 실제로 바꾸는 코드 경로는 SessionStore 하나뿐이다 (PLAN 철칙 1).

모든 상태 전이는 state_machine.validate_transition을 통과해야만 커밋되고,
통과하지 못하면 어떤 행도 변경되지 않는다 (트랜잭션 롤백).
모든 변경은 Event 테이블에 append-only로 남는다 (PLAN 철칙 3 — eval·디버깅·감사의 원천).

본개발 전환 시 SQLite → Postgres 교체 지점: db_url만 바꾸면 된다 (PLAN §11).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from core.orchestrator.state_machine import INITIAL_STATE, State, validate_transition

#: 프로젝트 루트 (core/orchestrator/session.py 기준 두 단계 위)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "sessions.db"
ENV_DB_VAR = "PRINT_INTAKE_DB"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def resolve_db_url(db_url: str | None = None) -> str:
    """DB URL 결정 우선순위: 명시 인자 > 환경변수 PRINT_INTAKE_DB > data/sessions.db.

    환경변수에 URL이 아닌 파일 경로가 들어오면 sqlite URL로 감싼다.
    """
    if db_url:
        return db_url
    env = os.environ.get(ENV_DB_VAR)
    if env:
        return env if "://" in env else f"sqlite:///{Path(env).as_posix()}"
    return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


class Base(DeclarativeBase):
    pass


class OrderSession(Base):
    """주문 세션 1건. slots는 {슬롯명: {value, source, confirmed, change_count}} JSON."""

    __tablename__ = "order_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    state: Mapped[str] = mapped_column(String(32), default=INITIAL_STATE.value)
    customer_type: Mapped[str | None] = mapped_column(String(8), nullable=True)  # A/B/C
    product: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    escalation_reasons: Mapped[list] = mapped_column(JSON, default=list)
    customer_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_parse_failures: Mapped[int] = mapped_column(Integer, default=0)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    # 시안 생성(명함) 경로 — 파일 없는 고객이 정보만으로 인쇄용 시안을 받는 흐름
    design_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    card_content: Mapped[dict] = mapped_column(JSON, default=dict)   # CardContent 필드
    card_template: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Event(Base):
    """세션별 append-only 이벤트 로그. seq는 세션 내 1부터 단조 증가."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("order_sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class SessionStore:
    """세션 상태 변경의 유일한 진입점.

    상태를 바꾸는 모든 코드는 이 클래스를 거친다. LLM 계층은 이 모듈을
    임포트할 수 없다 (의존 방향: llm은 제안만 반환, 적용은 오케스트레이터).
    """

    def __init__(self, db_url: str | None = None):
        url = resolve_db_url(db_url)
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite"):
            if ":memory:" in url:
                # 인메모리 SQLite는 커넥션마다 별개 DB가 되므로 단일 커넥션 고정
                kwargs["connect_args"] = {"check_same_thread": False}
                kwargs["poolclass"] = StaticPool
            else:
                # 파일 DB면 디렉터리를 미리 만들어 둔다 (기본 data/sessions.db)
                db_path = Path(url.removeprefix("sqlite:///"))
                db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, **kwargs)
        Base.metadata.create_all(self.engine)
        self._ensure_columns()  # 기존 파일 DB에 새 컬럼(시안 경로)을 더한다
        # expire_on_commit=False: 반환된 객체를 세션 종료 후에도 읽을 수 있게
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def _ensure_columns(self) -> None:
        """create_all은 테이블만 만들고 컬럼 추가는 못 한다. 스키마가 늘었을 때
        기존 SQLite 파일에 누락 컬럼을 ADD COLUMN으로 채운다 (프로토타입용 경량 마이그레이션).
        """
        from sqlalchemy import inspect, text

        added = {
            "design_mode": "BOOLEAN DEFAULT 0",
            "card_content": "JSON",
            "card_template": "VARCHAR(32)",
        }
        insp = inspect(self.engine)
        existing = {c["name"] for c in insp.get_columns("order_sessions")}
        with self.engine.begin() as conn:
            for name, ddl in added.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE order_sessions ADD COLUMN {name} {ddl}"))

    # ------------------------------------------------------------------ 내부

    def _db(self) -> Session:
        return self._session_factory()

    @staticmethod
    def _get_or_raise(db: Session, session_id: str) -> OrderSession:
        row = db.get(OrderSession, session_id)
        if row is None:
            raise KeyError(f"세션 없음: {session_id}")
        return row

    @staticmethod
    def _next_seq(db: Session, session_id: str) -> int:
        last = db.scalar(select(func.max(Event.seq)).where(Event.session_id == session_id))
        return (last or 0) + 1

    def _append_event(self, db: Session, session_id: str, type_: str, payload: dict) -> Event:
        """이벤트 append. 호출자와 같은 트랜잭션 안에서 실행 — 커밋도 호출자가 한다."""
        ev = Event(
            session_id=session_id,
            seq=self._next_seq(db, session_id),
            type=type_,
            payload=payload,
        )
        db.add(ev)
        return ev

    # ------------------------------------------------------------------ 조회

    def get(self, session_id: str) -> OrderSession | None:
        with self._db() as db:
            return db.get(OrderSession, session_id)

    def events(self, session_id: str) -> list[Event]:
        """세션의 이벤트 전체 (seq 오름차순)."""
        with self._db() as db:
            rows = db.scalars(
                select(Event).where(Event.session_id == session_id).order_by(Event.seq)
            )
            return list(rows)

    # ------------------------------------------------------------------ 변경

    def create_session(self) -> OrderSession:
        with self._db() as db:
            row = OrderSession(id=str(uuid.uuid4()))
            db.add(row)
            db.flush()  # id 확정 후 이벤트 기록
            self._append_event(db, row.id, "session_created", {"state": row.state})
            db.commit()
            return row

    def transition(self, session_id: str, target: State, reason: str = "") -> OrderSession:
        """상태 전이. state_machine 전이 테이블을 통과한 것만 커밋.

        불허 전이는 TransitionError를 던지며, 이때 상태·이벤트 어느 것도
        변경되지 않는다 (커밋 전에 예외 → with 블록 종료 시 롤백).
        """
        target = State(target)
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            current = State(row.state)
            validate_transition(current, target)  # 실패 시 여기서 예외 → 무변경
            row.state = target.value
            self._append_event(
                db,
                row.id,
                "transition",
                {"from": current.value, "to": target.value, "reason": reason},
            )
            db.commit()
            return row

    def set_slot(self, session_id: str, name: str, value: Any, source: str = "user") -> OrderSession:
        """슬롯 값 기록. source ∈ {user, inferred, default}.

        기존 값과 다른 값이 들어오면 change_count 증가 + confirmed 해제
        (값이 바뀌면 이전 확정은 무효). 같은 값 재설정은 카운트 미증가.
        change_count는 에스컬레이션 시그널(같은 슬롯 2회 이상 변경)의 입력이다.
        """
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            # JSON 컬럼은 in-place 변경이 감지되지 않으므로 새 dict를 재할당한다
            slots = dict(row.slots or {})
            prev = slots.get(name)
            if prev is None:
                entry = {"value": value, "source": source, "confirmed": False, "change_count": 0}
            else:
                entry = dict(prev)
                if entry.get("value") != value:
                    entry["change_count"] = int(entry.get("change_count", 0)) + 1
                    entry["confirmed"] = False
                entry["value"] = value
                entry["source"] = source
            slots[name] = entry
            row.slots = slots
            self._append_event(
                db,
                row.id,
                "slot_set",
                {
                    "slot": name,
                    "value": value,
                    "source": source,
                    "change_count": entry["change_count"],
                },
            )
            db.commit()
            return row

    def record_event(self, session_id: str, type_: str, payload: dict | None = None) -> Event:
        """임의 이벤트 기록 (LLM 호출 로그, 견적 산출 등 감사용)."""
        with self._db() as db:
            self._get_or_raise(db, session_id)
            ev = self._append_event(db, session_id, type_, payload or {})
            db.commit()
            return ev

    def escalate(self, session_id: str, reason: str) -> OrderSession:
        """에스컬레이션 플래그 + 사유 누적. (상태를 ESCALATED로 옮기는 것은
        별도의 transition 호출 — 플래그와 상태 전이를 분리해 둔다.)"""
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.escalated = True
            reasons = list(row.escalation_reasons or [])
            reasons.append(reason)
            row.escalation_reasons = reasons
            self._append_event(db, row.id, "escalated", {"reason": reason})
            db.commit()
            return row

    def confirm(self, session_id: str) -> OrderSession:
        """고객 확정 이벤트 — 3중 관문(production_gate)의 두 번째 조건."""
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.customer_confirmed = True
            self._append_event(db, row.id, "customer_confirmed", {})
            db.commit()
            return row

    # ------------------------------------------------ 카운터 (시그널 입력값)

    def increment_turn(self, session_id: str) -> OrderSession:
        """고객-AI 왕복 1회 기록 (에스컬레이션 시그널 '왕복>6'의 입력)."""
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.turn_count += 1
            db.commit()
            return row

    def record_llm_parse_failure(self, session_id: str) -> OrderSession:
        """LLM 출력 스키마 검증 실패 누적 (시그널 '2회 연속'의 입력)."""
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.llm_parse_failures += 1
            self._append_event(db, row.id, "llm_parse_failure", {"count": row.llm_parse_failures})
            db.commit()
            return row

    def reset_llm_parse_failures(self, session_id: str) -> OrderSession:
        """파싱 성공 시 연속 실패 카운터 리셋 ('연속' 의미 유지)."""
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.llm_parse_failures = 0
            db.commit()
            return row

    # ------------------------------------------------ 세션 메타 필드 갱신

    def set_customer_type(self, session_id: str, customer_type: str) -> OrderSession:
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.customer_type = customer_type
            self._append_event(db, row.id, "customer_type_set", {"customer_type": customer_type})
            db.commit()
            return row

    def set_product(self, session_id: str, product: str) -> OrderSession:
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.product = product
            self._append_event(db, row.id, "product_set", {"product": product})
            db.commit()
            return row

    def set_file_path(self, session_id: str, file_path: str) -> OrderSession:
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.file_path = file_path
            self._append_event(db, row.id, "file_attached", {"file_path": file_path})
            db.commit()
            return row

    def set_design(
        self, session_id: str, card_content: dict, template: str, mode: bool = True
    ) -> OrderSession:
        """시안 경로 상태 기록: 명함 콘텐츠 필드 + 선택 템플릿."""
        with self._db() as db:
            row = self._get_or_raise(db, session_id)
            row.design_mode = mode
            row.card_content = dict(card_content or {})
            row.card_template = template
            self._append_event(
                db,
                row.id,
                "design_updated",
                {"template": template, "fields": sorted(k for k, v in card_content.items() if v)},
            )
            db.commit()
            return row
