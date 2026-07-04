"""오케스트레이터 서비스 계층 — 상태를 바꾸는 유일한 두뇌.

턴 처리 파이프라인:
    (LLM 또는 규칙이 만든) 검증된 제안 → 스키마 대조 적용 → 파일 검판/견적 →
    질문 정책 → 에스컬레이션 시그널 → 상태 전이 → 지시서(directives) 반환

LLM은 여기서 나온 directives를 자연어로 번역할 뿐이다 (ADR-001).
cards의 숫자·상태는 전부 결정론적 엔진 출력이며 LLM을 거치지 않는다.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.autofix.extend_bleed import extend_bleed
from core.design.card import generate_namecard
from core.design.schema import DEFAULT_TEMPLATE, DESIGNABLE_PRODUCTS, TEMPLATES, CardContent
from core.llm.parsing import ClassifyProposal, CustomerType, Intent, SlotProposal
from core.orchestrator import policy
from core.orchestrator.policy import AutoFill, Conflict, SlotQuestion
from core.orchestrator.session import PROJECT_ROOT, OrderSession, SessionStore
from core.orchestrator.state_machine import State
from core.preflight.engine import OrderContext, run_preflight
from core.preflight.report import CheckStatus, PreflightReport
from core.products.schema import ProductSchema, load_catalog
from core.quote.engine import QuoteResult, quote

UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"
PREVIEW_DIR = PROJECT_ROOT / "data" / "previews"

#: 규격명 → 재단 mm. 카탈로그 size.choices의 "A5" 같은 이름을 mm로 푼다.
NAMED_SIZES_MM: dict[str, tuple[float, float]] = {
    "A1": (594, 841),
    "A2": (420, 594),
    "A3": (297, 420),
    "A4": (210, 297),
    "A5": (148, 210),
    "B5": (182, 257),
}
SIZE_MATCH_TOLERANCE_MM = 1.0


def choice_to_mm(choice: str) -> tuple[float, float] | None:
    """사이즈 선택지 문자열 → (w, h) mm. "90x90" 또는 "A5" 형식."""
    if choice in NAMED_SIZES_MM:
        return NAMED_SIZES_MM[choice]
    try:
        w, h = choice.lower().split("x")
        return (float(w), float(h))
    except (ValueError, AttributeError):
        return None


def match_size_choice(schema: ProductSchema, w_mm: float, h_mm: float) -> str | None:
    """파일 재단 크기 → 카탈로그 사이즈 선택지 (회전 일치 포함, ±1mm)."""
    size_slot = schema.slots.get("size")
    if size_slot is None:
        return None
    for choice in size_slot.choices:
        target = choice_to_mm(str(choice))
        if target is None:
            continue
        tw, th = target
        t = SIZE_MATCH_TOLERANCE_MM
        if (abs(w_mm - tw) <= t and abs(h_mm - th) <= t) or (
            abs(w_mm - th) <= t and abs(h_mm - tw) <= t
        ):
            return str(choice)
    return None


# ---------------------------------------------------------------- 반환 모델


class SessionView(BaseModel):
    id: str
    state: str
    product: str | None = None
    customer_type: str | None = None
    slots: dict[str, Any] = Field(default_factory=dict)
    escalated: bool = False
    confirmed: bool = False
    turn_count: int = 0
    file_path: str | None = None
    design_mode: bool = False
    card_template: str | None = None


class ReplyDirectives(BaseModel):
    """LLM(또는 템플릿 폴백)이 자연어 응답을 만들 재료. 숫자·판정은 전부 확정값."""

    kind: str = "turn"  # greeting | turn | upload | autofix | confirm | design
    questions: list[SlotQuestion] = Field(default_factory=list)
    auto_filled: list[AutoFill] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)   # 기계 코드 목록 (예: invalid_value:size=75)
    request_product: bool = False
    request_file: bool = False
    offer_autofix: list[str] = Field(default_factory=list)  # 체크 id
    report: PreflightReport | None = None
    quote: QuoteResult | None = None
    gate_blockers: list[str] = Field(default_factory=list)
    escalation_reasons: list[str] = Field(default_factory=list)
    order_no: str | None = None
    awaiting_confirm: bool = False
    # 시안 생성 경로
    design_generated: bool = False           # 이번 턴에 시안을 새로 만들었는가
    design_template_name: str = ""           # 적용된 템플릿 표시명
    request_card_fields: bool = False        # 이름/회사 등 명함 내용이 더 필요
    offer_design: bool = False               # 파일 없는 명함 고객에게 시안 생성 제안


class TurnResult(BaseModel):
    session: SessionView
    directives: ReplyDirectives
    cards: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------- 서비스


class IntakeService:
    def __init__(self, store: SessionStore | None = None, catalog: dict[str, ProductSchema] | None = None):
        self.store = store or SessionStore()
        self.catalog = catalog or load_catalog()

    # ------------------------------------------------------------ 공개 API

    def start(self) -> TurnResult:
        row = self.store.create_session()
        d = ReplyDirectives(kind="greeting", request_product=True, request_file=True)
        return TurnResult(session=self._view(row), directives=d, cards=[])

    def apply_turn(
        self,
        session_id: str,
        classify: ClassifyProposal | None = None,
        proposal: SlotProposal | None = None,
    ) -> TurnResult:
        """대화 1턴 적용. 제안(classify/proposal)은 이미 pydantic 검증을 통과한 것."""
        self.store.increment_turn(session_id)
        row = self._get(session_id)
        notices: list[str] = []

        # INTAKE → CLASSIFY: 첫 입력이 들어오는 순간
        if State(row.state) == State.INTAKE:
            row = self.store.transition(session_id, State.CLASSIFY, "first_input")

        # 분류 제안 적용
        if classify is not None:
            if classify.product and classify.product in self.catalog:
                if row.product != classify.product:
                    row = self.store.set_product(session_id, classify.product)
            elif classify.product:
                notices.append(f"unknown_product:{classify.product}")
            if classify.customer_type and row.customer_type != classify.customer_type.value:
                row = self.store.set_customer_type(session_id, classify.customer_type.value)
                if classify.customer_type == CustomerType.C:
                    # 시안 제작(백지 고객)은 프로토타입 스코프 밖 — 사람에게
                    self.store.escalate(session_id, "customer_type_C_design_needed")

        # CLASSIFY → SLOT_FILLING: 상품이 정해지면
        row = self._get(session_id)
        if State(row.state) == State.CLASSIFY and row.product:
            row = self.store.transition(session_id, State.SLOT_FILLING, "product_identified")

        # 슬롯 제안 적용 (스키마 대조 — 통과한 값만)
        negative = False
        wants_confirm = False
        if proposal is not None:
            negative = proposal.negative_sentiment or proposal.intent == Intent.COMPLAINT
            wants_confirm = proposal.intent == Intent.CONFIRM
            if row.product:
                schema = self.catalog[row.product]
                for name, value in proposal.slots.items():
                    ok, resolved = self._validate_slot_value(schema, name, value)
                    if ok:
                        self.store.set_slot(session_id, name, resolved, source="user")
                    else:
                        notices.append(f"invalid_value:{name}={value}")
            elif proposal.slots:
                notices.append("slots_before_product")

        # 확정 의사가 확정 단계에서 나오면 확정 처리로 위임
        row = self._get(session_id)
        if wants_confirm and State(row.state) == State.PROOF_CONFIRM:
            return self.confirm(session_id)

        return self._advance(session_id, notices=notices, negative_sentiment=negative)

    def handle_upload(self, session_id: str, src_path: str | Path, original_name: str = "") -> TurnResult:
        """PDF 업로드 → 보관 → (가능하면) FILE_CHECK 전이 → 프리플라이트 → 정책 재평가."""
        self.store.increment_turn(session_id)
        row = self._get(session_id)

        dest_dir = UPLOAD_DIR / session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (Path(original_name).name or Path(src_path).name or "upload.pdf")
        if Path(src_path).resolve() != dest.resolve():
            shutil.copy2(src_path, dest)
        self.store.set_file_path(session_id, str(dest))

        if State(row.state) == State.INTAKE:
            self.store.transition(session_id, State.CLASSIFY, "file_first")
        row = self._get(session_id)

        # 정식 검판 전이는 SLOT_FILLING/PROOF_CONFIRM에서만 가능 (상품 미정이면 비공식 검판)
        formal = State(row.state) in (State.SLOT_FILLING, State.PROOF_CONFIRM)
        if formal:
            self.store.transition(session_id, State.FILE_CHECK, "file_uploaded")

        report = self._run_preflight(session_id)

        if formal:
            self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")

        notices = [] if row.product else ["file_received_need_product"]
        return self._advance(session_id, notices=notices, kind="upload", report=report)

    def handle_autofix(self, session_id: str, check_id: str) -> TurnResult:
        """autofix 적용 (현재 bleed 1종). 고친 파일로 교체 후 재검판."""
        row = self._get(session_id)
        if check_id != "bleed":
            return self._advance(session_id, notices=[f"autofix_unsupported:{check_id}"])
        if not row.file_path:
            return self._advance(session_id, notices=["autofix_no_file"])

        src = Path(row.file_path)
        fixed = src.parent / f"{src.stem}_fixed.pdf"
        pv_dir = PREVIEW_DIR / session_id
        fix_result = extend_bleed(src, fixed, bleed_mm=3.0, preview_dir=pv_dir)
        self.store.set_file_path(session_id, str(fixed))
        self.store.record_event(session_id, "autofix_applied", {"check_id": check_id, **fix_result})

        if State(row.state) in (State.SLOT_FILLING, State.PROOF_CONFIRM):
            self.store.transition(session_id, State.FILE_CHECK, "autofix_recheck")
            report = self._run_preflight(session_id)
            self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")
        else:
            report = self._run_preflight(session_id)

        result = self._advance(session_id, kind="autofix", report=report)
        previews = fix_result.get("previews") or []
        if previews:
            result.cards.insert(
                0,
                {
                    "type": "autofix_preview",
                    "check_id": check_id,
                    "before": previews[0]["before"],
                    "after": previews[0]["after"],
                },
            )
        return result

    def handle_card_content(
        self,
        session_id: str,
        content: CardContent,
        template: str | None = None,
    ) -> TurnResult:
        """명함 시안 경로: 내용 필드를 누적하고, 충분하면 인쇄용 시안을 생성한다.

        생성물은 업로드 파일과 동일한 프리플라이트·견적·확정 파이프라인을 탄다
        (생성이든 업로드든 같은 검판을 거친다는 신뢰가 데모의 핵심).
        """
        self.store.increment_turn(session_id)
        row = self._get(session_id)

        # 상품 확정: 시안 생성은 명함만 (DESIGNABLE_PRODUCTS)
        if row.product not in DESIGNABLE_PRODUCTS:
            if row.product is None:
                self.store.set_product(session_id, "namecard")
            else:
                return self._advance(session_id, notices=[f"design_unsupported:{row.product}"])

        # 상태 전진: INTAKE→CLASSIFY→SLOT_FILLING
        row = self._get(session_id)
        if State(row.state) == State.INTAKE:
            self.store.transition(session_id, State.CLASSIFY, "design_first_input")
        row = self._get(session_id)
        if State(row.state) == State.CLASSIFY:
            self.store.transition(session_id, State.SLOT_FILLING, "design_product_namecard")

        # 내용 누적 (턴마다 부분 입력을 합친다)
        existing = CardContent(**(row.card_content or {}))
        merged = existing.merged_with(content)
        tmpl = template or row.card_template or DEFAULT_TEMPLATE
        if tmpl not in TEMPLATES:
            tmpl = DEFAULT_TEMPLATE
        self.store.set_design(session_id, merged.model_dump(), tmpl)

        # 이름·회사 중 하나도 없으면 아직 못 만든다 → 내용 요청
        if not merged.is_generatable():
            result = self._advance(session_id, kind="design")
            result.directives.request_card_fields = True
            return result

        # 생성 → 세션 파일로 등록 → 검판
        self._generate_design(session_id, merged, tmpl)
        result = self._advance(session_id, kind="design", report=self._latest_report(session_id))
        result.directives.design_generated = True
        result.directives.design_template_name = TEMPLATES[tmpl]
        row = self._get(session_id)
        result.cards.insert(0, self._design_card(row))
        return result

    def _generate_design(self, session_id: str, content: CardContent, template: str) -> dict:
        """명함 PDF 생성 → 파일 등록 → FILE_CHECK 왕복으로 검판."""
        out_dir = UPLOAD_DIR / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "design_namecard.pdf"
        info = generate_namecard(content, out, template=template)
        self.store.set_file_path(session_id, str(out))
        self.store.record_event(session_id, "design_generated", info)

        row = self._get(session_id)
        if State(row.state) in (State.SLOT_FILLING, State.PROOF_CONFIRM):
            self.store.transition(session_id, State.FILE_CHECK, "design_recheck")
            self._run_preflight(session_id)
            self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")
        else:
            self._run_preflight(session_id)
        return info

    def _design_card(self, row: OrderSession) -> dict:
        """design_preview 카드 (API가 preview 로컬경로 → URL 변환)."""
        preview = self._render_preview_png(Path(row.file_path), row.id) if row.file_path else None
        return {
            "type": "design_preview",
            "template": row.card_template,
            "templates": [{"id": tid, "name": name} for tid, name in TEMPLATES.items()],
            "preview": preview,
            "fields": {k: v for k, v in (row.card_content or {}).items() if v},
        }

    def _render_preview_png(self, pdf_path: Path, session_id: str, scale: float = 2.5) -> str | None:
        """PDF 1페이지 → 미리보기 PNG (pdfium). previews 디렉터리에 저장."""
        try:
            import pypdfium2 as pdfium

            out_dir = PREVIEW_DIR / session_id
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{pdf_path.stem}_preview.png"
            doc = pdfium.PdfDocument(str(pdf_path))
            try:
                img = doc[0].render(scale=scale).to_pil()
            finally:
                doc.close()
            img.save(out)
            return str(out)
        except Exception:
            return None

    def confirm(self, session_id: str) -> TurnResult:
        """고객 확정 → 3중 관문 → 통과 시 결제 목업 → 주문 완료."""
        row = self._get(session_id)
        if State(row.state) != State.PROOF_CONFIRM:
            return self._advance(session_id, notices=["confirm_not_ready"])

        self.store.confirm(session_id)
        row = self._get(session_id)
        report = self._latest_report(session_id)
        gate = policy.production_gate(report, row.customer_confirmed, row.escalated)
        self.store.record_event(session_id, "gate_check", gate.model_dump())

        if not gate.ok:
            result = self._advance(session_id, kind="confirm")
            result.directives.gate_blockers = gate.blockers
            return result

        self.store.transition(session_id, State.PAYMENT_MOCK, "gate_passed")
        order_no = f"PL-{session_id[:8].upper()}"
        self.store.record_event(session_id, "payment_mock", {"order_no": order_no})
        self.store.transition(session_id, State.COMPLETED, "payment_mock_done")

        row = self._get(session_id)
        quote_result = self._quote(row)
        d = ReplyDirectives(kind="confirm", order_no=order_no, quote=quote_result)
        cards = [
            {
                "type": "order_confirmed",
                "order_no": order_no,
                "summary": {
                    "product": row.product,
                    "slots": {k: v.get("value") for k, v in (row.slots or {}).items()},
                    "total": quote_result.total if quote_result else None,
                },
            }
        ]
        return TurnResult(session=self._view(row), directives=d, cards=cards)

    def view_session(self, session_id: str) -> SessionView:
        return self._view(self._get(session_id))

    def transcript(self, session_id: str) -> list[dict]:
        """이벤트 로그 (감사·디버그·데모 타임라인용)."""
        return [
            {"seq": e.seq, "ts": e.ts.isoformat(), "type": e.type, "payload": e.payload}
            for e in self.store.events(session_id)
        ]

    # ------------------------------------------------------------ 내부

    def _get(self, session_id: str) -> OrderSession:
        row = self.store.get(session_id)
        if row is None:
            raise KeyError(f"세션 없음: {session_id}")
        return row

    def _view(self, row: OrderSession) -> SessionView:
        return SessionView(
            id=row.id,
            state=row.state,
            product=row.product,
            customer_type=row.customer_type,
            slots=row.slots or {},
            escalated=row.escalated,
            confirmed=row.customer_confirmed,
            turn_count=row.turn_count,
            file_path=row.file_path,
            design_mode=bool(row.design_mode),
            card_template=row.card_template,
        )

    def _validate_slot_value(self, schema: ProductSchema, name: str, value: Any) -> tuple[bool, Any]:
        """제안된 슬롯 값 검증: 스키마에 없는 슬롯·선택지는 거부, synonyms는 정규화."""
        sdef = schema.slots.get(name)
        if sdef is None:
            return False, None
        # synonyms 정규화 (고객 언어 → 스펙)
        if isinstance(value, str) and value in sdef.synonyms:
            value = sdef.synonyms[value]
        if name == "quantity":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return False, None
            return (value > 0), value
        if sdef.choices:
            if str(value) in [str(c) for c in sdef.choices]:
                return True, value
            return False, None
        return True, value

    def _expected_pages(self, row: OrderSession) -> int:
        """주문 기준 기대 페이지 수: 양면(double)이면 2, 아니면 1."""
        slots = row.slots or {}
        sides = (slots.get("sides") or {}).get("value")
        return 2 if sides == "double" else 1

    def _order_context(self, row: OrderSession) -> OrderContext:
        size_mm = None
        size_value = (row.slots or {}).get("size", {}).get("value") if row.slots else None
        # 사용자가 말한(또는 이전에 확정된) 사이즈만 주문 기준으로 삼는다.
        # 파일에서 추론한 사이즈로 파일 자신을 검사하는 것은 무의미하므로 source=inferred는 제외.
        if size_value and (row.slots["size"].get("source") != "inferred"):
            size_mm = choice_to_mm(str(size_value))
        return OrderContext(
            product=row.product,
            size_mm=size_mm,
            page_count=self._expected_pages(row) if row.product else None,
        )

    def _run_preflight(self, session_id: str) -> PreflightReport:
        row = self._get(session_id)
        report = run_preflight(row.file_path, self._order_context(row))
        self.store.record_event(session_id, "preflight_report", report.model_dump(mode="json"))
        return report

    def _latest_report(self, session_id: str) -> PreflightReport | None:
        for e in reversed(self.store.events(session_id)):
            if e.type == "preflight_report":
                return PreflightReport.model_validate(e.payload)
        return None

    def _inferred(self, row: OrderSession, report: PreflightReport | None) -> dict[str, Any]:
        """측정값 → 슬롯 추론값 변환 (infer_from 선언 기반)."""
        if row.product is None or report is None:
            return {}
        schema = self.catalog[row.product]
        inferred: dict[str, Any] = {}
        for name, sdef in schema.slots.items():
            for source in sdef.infer_from:
                if source == "file_trimbox":
                    ps = report.by_id("page_size")
                    file_size = (ps.measured or {}).get("file_size_mm") if ps else None
                    if not file_size:
                        bl = report.by_id("bleed")
                        file_size = (bl.measured or {}).get("trim_size_mm") if bl else None
                    if file_size and len(file_size) == 2:
                        match = match_size_choice(schema, float(file_size[0]), float(file_size[1]))
                        if match:
                            inferred[name] = match
                elif source == "dieline_present":
                    dl = report.by_id("dieline")
                    if dl and (dl.measured or {}).get("dieline_present"):
                        inferred[name] = "die_cut"
                elif source == "file_page_count":
                    pc = report.by_id("page_count")
                    file_pages = (pc.measured or {}).get("file_pages") if pc else None
                    if file_pages in (1, 2):
                        inferred[name] = "single" if file_pages == 1 else "double"
        return inferred

    def _quote(self, row: OrderSession) -> QuoteResult | None:
        if not row.product:
            return None
        values = {k: v.get("value") for k, v in (row.slots or {}).items() if v.get("value") is not None}
        return quote(row.product, values)

    def _advance(
        self,
        session_id: str,
        notices: list[str] | None = None,
        negative_sentiment: bool = False,
        kind: str = "turn",
        report: PreflightReport | None = None,
    ) -> TurnResult:
        """턴 마무리 공통 파이프라인: 추론 반영 → 질문 정책 → 견적 → 시그널 → 전이 → 지시서."""
        row = self._get(session_id)
        d = ReplyDirectives(kind=kind, notices=list(notices or []))

        if report is None:
            report = self._latest_report(session_id)
        d.report = report

        if not row.product:
            d.request_product = True
            return TurnResult(session=self._view(row), directives=d, cards=self._cards(d, row))

        schema = self.catalog[row.product]
        inferred = self._inferred(row, report)

        # 추론값 영속화 — 사용자 값은 덮지 않지만, 기본값은 파일 증거가 이긴다
        for name, value in inferred.items():
            entry = (row.slots or {}).get(name, {})
            current, src = entry.get("value"), entry.get("source")
            if current is None or (src == "default" and current != value):
                self.store.set_slot(session_id, name, value, source="inferred")
        row = self._get(session_id)

        decision = policy.next_actions(schema, row.slots or {}, inferred, report)
        d.questions = decision.questions
        d.conflicts = decision.conflicts

        # 자동 채움 적용 + 통보
        for af in decision.auto_filled:
            self.store.set_slot(session_id, af.slot, af.value, source="default")
        d.auto_filled = decision.auto_filled
        row = self._get(session_id)

        # 견적 (필수 슬롯이 다 찼을 때만)
        required_missing = [
            n for n, sd in schema.required_slots().items()
            if (row.slots or {}).get(n, {}).get("value") is None
        ]
        quote_result: QuoteResult | None = None
        if not required_missing:
            quote_result = self._quote(row)
            if quote_result is not None:
                self.store.record_event(
                    session_id,
                    "quote",
                    {"total": quote_result.total, "missing": quote_result.missing},
                )
                if quote_result.missing:
                    d.notices.extend(f"quote_missing:{m}" for m in quote_result.missing)
                else:
                    d.quote = quote_result

        # 에스컬레이션 시그널
        change_counts = {k: v.get("change_count", 0) for k, v in (row.slots or {}).items()}
        signals = policy.escalation_signals(
            turn_count=row.turn_count,
            slot_change_counts=change_counts,
            report=report,
            quote_total=quote_result.total if (quote_result and not quote_result.missing) else None,
            llm_parse_failures=row.llm_parse_failures,
            negative_sentiment=negative_sentiment,
        )
        existing = set(row.escalation_reasons or [])
        for sig in signals:
            if sig not in existing:
                self.store.escalate(session_id, sig)
        row = self._get(session_id)
        d.escalation_reasons = list(row.escalation_reasons or [])

        # autofix 제안 (fail이면서 자동 수정 가능한 체크)
        if report is not None:
            d.offer_autofix = [
                r.check_id
                for r in report.results
                if r.status == CheckStatus.FAIL and r.autofix.available
            ]

        # 상태 전진: 질문·충돌 없음 ∧ 파일 검판 clean → PROOF_CONFIRM
        state = State(row.state)
        ready = (
            not d.questions
            and not d.conflicts
            and not required_missing
            and report is not None
            and report.gate_ok
            and quote_result is not None
            and not quote_result.missing
        )
        if state == State.SLOT_FILLING:
            if ready:
                row = self.store.transition(session_id, State.PROOF_CONFIRM, "all_slots_and_checks_ok")
                d.awaiting_confirm = True
            elif not row.file_path:
                # 명함은 파일이 없으면 시안 생성을 제안한다 (파일 없는 초보 고객 유입)
                if row.product in DESIGNABLE_PRODUCTS and not row.design_mode:
                    d.offer_design = True
                else:
                    d.request_file = True
        elif state == State.PROOF_CONFIRM:
            if ready:
                d.awaiting_confirm = True
            else:
                # 확정 단계에서 조건이 깨졌으면 슬롯 수집으로 복귀
                row = self.store.transition(session_id, State.SLOT_FILLING, "proof_conditions_broken")

        return TurnResult(session=self._view(row), directives=d, cards=self._cards(d, row))

    def _cards(self, d: ReplyDirectives, row: OrderSession) -> list[dict]:
        """directives → UI 카드 목록 (docs/API.md 계약)."""
        cards: list[dict] = []
        if d.report is not None and d.kind in ("upload", "autofix"):
            cards.append(
                {
                    "type": "preflight_report",
                    "results": [r.model_dump(mode="json") for r in d.report.results],
                    "gate_ok": d.report.gate_ok,
                }
            )
        if d.quote is not None and not d.quote.missing:
            cards.append({"type": "quote", **d.quote.model_dump(mode="json")})
        if d.escalation_reasons:
            cards.append({"type": "escalation", "reasons": d.escalation_reasons})
        if d.order_no:
            pass  # order_confirmed 카드는 confirm()에서 직접 구성
        return cards
