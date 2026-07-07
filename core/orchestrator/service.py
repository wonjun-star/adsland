"""오케스트레이터 서비스 계층 — 상태를 바꾸는 유일한 두뇌.

턴 처리 파이프라인:
    (LLM 또는 규칙이 만든) 검증된 제안 → 스키마 대조 적용 → 파일 검판/견적 →
    질문 정책 → 에스컬레이션 시그널 → 상태 전이 → 지시서(directives) 반환

LLM은 여기서 나온 directives를 자연어로 번역할 뿐이다 (ADR-001).
cards의 숫자·상태는 전부 결정론적 엔진 출력이며 LLM을 거치지 않는다.
"""

from __future__ import annotations

import re
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


#: "○○ 뭐 있어?"에서 슬롯을 알아채는 키워드 (고객 언어 → 슬롯)
_SLOT_ASK_KEYWORDS: dict[str, str] = {
    "용지": "material", "종이": "material", "재질": "material",
    "코팅": "coating", "라미네이팅": "coating",
    "사이즈": "size", "크기": "size", "규격": "size",
    "재단": "cut_type", "도무송": "cut_type", "칼선": "cut_type",
    "후가공": "finishing",
    "수량": "quantity", "매수": "quantity",
}
#: 옵션을 묻는 말투 (이게 있어야 '선택지 보여달라'는 뜻으로 본다)
_OPTION_ASK_PHRASES = ("있어", "있나", "있을까", "있는", "종류", "뭐", "무슨", "어떤", "옵션", "골라", "선택", "뭔가")


#: 고객이 '탐색·질문' 중임을 나타내는 말투 (이런 턴엔 견적·주문확인 카드를 다시 띄우지 않는다)
_EXPLORE_MARKERS = (
    "뭐있", "뭐가있", "말고", "각각", "비교", "옵션", "종류", "얼마", "차이", "다른거", "어떤게",
)


def _looks_exploring(text: str | None) -> bool:
    """고객이 아직 고르는 중(옵션·가격을 묻는 중)인가 — 확정 카드 재노출 여부 판단용."""
    if not text:
        return False
    low = text.replace(" ", "")
    return any(m in low for m in _EXPLORE_MARKERS)


#: 고객이 '이제 최종 견적/확인을 보겠다'는 신호 (이때만 견적·확인 카드를 띄운다)
_FINAL_REVIEW_MARKERS = (
    "최종견적", "견적볼", "견적보여", "견적확인", "최종확인", "확인할게", "확인해줘",
    "이제됐", "다정했", "다됐어", "없어요", "없습니다", "없어", "없네", "그대로진행", "진행할게",
)


def _wants_final_review(text: str | None) -> bool:
    """'최종 견적 볼게요 / 없어요(더 바꿀 것) / 그대로 진행' 처럼 최종 확인을 요청했는가."""
    if not text:
        return False
    low = text.replace(" ", "")
    return any(m in low for m in _FINAL_REVIEW_MARKERS)


def _asked_options_slot(text: str, schema: ProductSchema | None) -> str | None:
    """'용지 뭐 있어?'처럼 특정 슬롯의 선택지를 묻는 말이면 그 슬롯명을 돌려준다."""
    if not text or schema is None:
        return None
    low = text.replace(" ", "")
    if not any(p in low for p in _OPTION_ASK_PHRASES):
        return None
    for kw, slot in _SLOT_ASK_KEYWORDS.items():
        if kw in low and slot in schema.slots and schema.slots[slot].choices:
            return slot
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
    request_cutline: bool = False  # 도무송인데 칼선 없음 → 칼선 파일 별도 요청
    offer_autofix: list[str] = Field(default_factory=list)  # 체크 id
    report: PreflightReport | None = None
    quote: QuoteResult | None = None
    gate_blockers: list[str] = Field(default_factory=list)
    escalation_reasons: list[str] = Field(default_factory=list)
    order_no: str | None = None
    awaiting_confirm: bool = False
    # 사양이 다 됐지만, 바로 계약서(견적·확인 카드)를 들이밀지 않는다.
    # offer_final_review=True → "더 바꿀 내용 있으세요? 없으면 최종 견적 보여드릴게요" (버튼만).
    # show_final=True → 이번 턴에 고객이 최종 견적을 요청함 → 견적·확인 카드를 띄운다.
    offer_final_review: bool = False
    show_final: bool = False
    # 최종 확인 체크리스트 (맥도날드 키오스크식) — 각 항목 확인 후 '이대로 주문'
    confirm_review: list[dict] = Field(default_factory=list)
    # 시안 생성 경로
    design_generated: bool = False           # 이번 턴에 시안을 새로 만들었는가
    design_template_name: str = ""           # 적용된 템플릿 표시명
    request_card_fields: bool = False        # 이름/회사 등 명함 내용이 더 필요
    offer_design: bool = False               # 파일 없는 명함 고객에게 시안 생성 제안
    # 결과 우선 — 예상 견적 + 변경 이력 (검판원·발주·고객 공용)
    estimate: bool = False                   # quote가 확정이 아니라 예상 견적
    changes: list[dict] = Field(default_factory=list)  # 접수본→최종본 변경 항목
    detected_product: str = ""               # 파일 규격으로 상품을 알아챘을 때 표시명
    offer_back_side: bool = False            # 앞면만 올린 명함류 → 뒷면(양면) 확인
    customer_question: str = ""              # 고객이 던진 질문 (용지·사이즈·가격 등) → 답해야 함
    customer_message: str = ""               # 고객이 방금 한 말 원문 (LLM이 의도 파악에 사용)
    asked_slot: str = ""                      # 고객이 "뭐 있어?"로 물은 슬롯 → 그 선택지를 버튼으로
    # 옵션별 실제 계산 가격 (지어내지 않게 미리 산출) — {슬롯: {"unit","quantity","choices":[{value,total}]}}
    option_prices: dict[str, Any] = Field(default_factory=dict)


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
        customer_text: str = "",
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
                    # 상품이 바뀌면(추정이 틀려 고객이 바로잡는 경우 포함) 사양 초기화 + 재검판
                    had_product = row.product is not None
                    self.store.set_product(session_id, classify.product)
                    if had_product:
                        self._reclassify_reset(session_id)
                    row = self._get(session_id)
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
        order_slot_changed = False  # size/sides/cut_type 등 검판 기준이 바뀌면 재검판
        if proposal is not None:
            negative = proposal.negative_sentiment or proposal.intent == Intent.COMPLAINT
            wants_confirm = proposal.intent == Intent.CONFIRM
            if row.product:
                schema = self.catalog[row.product]
                for name, value in proposal.slots.items():
                    ok, resolved = self._validate_slot_value(schema, name, value)
                    if ok:
                        before = (row.slots or {}).get(name, {}).get("value")
                        self.store.set_slot(session_id, name, resolved, source="user")
                        if name in ("size", "sides", "cut_type") and before != resolved:
                            order_slot_changed = True
                    else:
                        notices.append(f"invalid_value:{name}={value}")
            elif proposal.slots:
                notices.append("slots_before_product")

        # 검판 기준(재단 형태·크기·인쇄면)이 바뀌었고 파일이 있으면 재검판
        # → 예: 칼선 없는 스티커에 "사각재단" 답하면 칼선 항목이 통과로 바뀐다 (사람 안 거침)
        row = self._get(session_id)
        if order_slot_changed and row.file_path and State(row.state) == State.SLOT_FILLING:
            self.store.transition(session_id, State.FILE_CHECK, "order_slot_changed_recheck")
            self._run_preflight(session_id)
            self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")

        # 확정 의사가 확정 단계에서 나오면 확정 처리로 위임
        row = self._get(session_id)
        if wants_confirm and State(row.state) == State.PROOF_CONFIRM:
            return self.confirm(session_id)

        return self._advance(
            session_id, notices=notices, negative_sentiment=negative, customer_text=customer_text
        )

    def select_option(self, session_id: str, slot: str, value: Any) -> TurnResult:
        """질문 옵션 버튼 클릭 → 해당 슬롯을 바로 설정 (자유 입력 NLU 우회, 정확).

        '기타'로 자유 입력하면 apply_turn(NLU)이 처리하고, 목록 클릭은 이 경로로 온다.
        """
        self.store.increment_turn(session_id)
        row = self._get(session_id)
        if not row.product:
            return self._advance(session_id, notices=["slots_before_product"])
        schema = self.catalog[row.product]
        ok, resolved = self._validate_slot_value(schema, slot, value)
        if not ok:
            return self._advance(session_id, notices=[f"invalid_value:{slot}={value}"])

        before = (row.slots or {}).get(slot, {}).get("value")
        self.store.set_slot(session_id, slot, resolved, source="user")

        # 재단 형태·크기·인쇄면이 바뀌면 파일 재검판 (칼선 판정 등 즉시 갱신)
        row = self._get(session_id)
        if slot in ("size", "sides", "cut_type") and before != resolved and row.file_path:
            if State(row.state) == State.SLOT_FILLING:
                self.store.transition(session_id, State.FILE_CHECK, "select_recheck")
                self._run_preflight(session_id)
                self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")
        return self._advance(session_id)

    def reopen_slot(self, session_id: str, slot: str) -> TurnResult:
        """최종 확인에서 '바꾸기' 누른 항목을 다시 고르게 — 그 슬롯 선택지를 버튼으로 띄운다."""
        row = self._get(session_id)
        if not row.product:
            return self._advance(session_id)
        schema = self.catalog[row.product]
        sdef = schema.slots.get(slot)
        if sdef is None or not sdef.choices:
            return self._advance(session_id)
        opts = list(sdef.quick_options) or list(sdef.choices)
        d = ReplyDirectives(
            kind="turn",
            asked_slot=slot,
            questions=[
                SlotQuestion(
                    slot=slot,
                    display_name=sdef.display_name or slot,
                    reason="reopen_from_confirm",
                    quick_options=list(sdef.quick_options),
                    options=opts,
                    allow_other=True,
                )
            ],
        )
        return TurnResult(session=self._view(row), directives=d, cards=self._cards(d, row))

    def handle_upload(self, session_id: str, src_path: str | Path, original_name: str = "") -> TurnResult:
        """PDF/이미지 업로드 → 보관 → (가능하면) FILE_CHECK 전이 → 프리플라이트 → 정책 재평가.

        JPG/PNG는 주문 규격 크기의 PDF로 감싸 검수한다 (해상도·크기 검수 유효, 벡터 검수는 해당 없음).
        """
        self.store.increment_turn(session_id)
        row = self._get(session_id)

        # 이미지(JPG/PNG)면 PDF로 감싼다 — 주문 규격을 알면 그 크기로(해상도 검사가 실제 dpi를 잼)
        src_path = Path(src_path)
        image_wrapped = False
        try:
            head = src_path.read_bytes()[:16]
        except Exception:
            head = b""
        from core.intake.image_to_pdf import is_image_bytes

        eps_no_gs = False
        if is_image_bytes(head):
            from core.intake.image_to_pdf import EpsNeedsGhostscript, image_to_pdf

            order_size = self._order_context(row).size_mm
            pdf_path = src_path.with_name(src_path.stem + "_img.pdf")
            try:
                image_to_pdf(src_path, pdf_path, size_mm=order_size)
                src_path = pdf_path
                image_wrapped = True
            except EpsNeedsGhostscript:
                eps_no_gs = True  # 서버에 Ghostscript 없음 → 안내 (PDF로 저장 요청)
            except Exception:
                pass  # 변환 실패 시 원본 그대로 진행 (아래 프리플라이트가 판단 불가로 격리)
        if eps_no_gs:
            return self._advance(session_id, notices=["eps_needs_ghostscript"], kind="upload")

        prev_file = row.file_path  # 뒷면 병합 판단용 (앞면)

        dest_dir = UPLOAD_DIR / session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        src_path = Path(src_path)
        # 매 업로드를 고유 경로에 둔다 — 같은 파일명이어도 앞 파일을 덮지 않게 (뒷면 병합의 전제).
        if src_path.parent.resolve() == dest_dir.resolve():
            dest = src_path  # 이미 세션 폴더의 고유 경로(API가 저장) → 그대로 사용
        else:
            seq = len(list(dest_dir.glob("up_*")))
            safe = (Path(original_name).name or src_path.name or "upload.pdf").replace("/", "_")
            dest = dest_dir / f"up_{seq:02d}_{safe}"
            shutil.copy2(src_path, dest)

        # 뒷면 병합: 앞면 1장이 이미 있고, 이번이 두 번째 파일이면 뒷면으로 보고 2페이지로 합친다.
        # "양면"이라 말하지 않아도 — 파일을 둘 올린다는 것 자체가 앞뒤 신호다(명함·엽서 등 양면 상품).
        # 이미 양면으로 정했으면 당연히 병합.
        merged_back = False
        sides_val = (row.slots or {}).get("sides", {}).get("value")
        back_side_product = bool(row.product) and self.catalog[row.product].slots.get("sides") is not None
        # 두 번째 파일 = 뒷면(양면)으로 본다. 단, 앞면에 '재업로드가 필요한' 문제(저해상도·폰트 등
        # 자동 보정 불가·확인 필요)가 있었으면 두 번째는 '고쳐서 재업로드'로 보고 교체한다.
        # 여백처럼 자동 보정 가능한 문제는 앞면으로 인정하고 뒷면 병합을 진행한다.
        prev_report = self._latest_report(session_id)
        prev_needs_reupload = prev_report is not None and any(
            r.status == CheckStatus.UNCERTAIN
            or (r.status == CheckStatus.FAIL and not r.autofix.available)
            for r in prev_report.results
        )
        if (
            prev_file
            and Path(prev_file).exists()
            and Path(prev_file).resolve() != dest.resolve()
            and self._page_count(prev_file) == 1
            and (sides_val == "double" or (back_side_product and not prev_needs_reupload))
        ):
            combined = dest_dir / "combined_front_back.pdf"
            if self._merge_front_back(Path(prev_file), dest, combined):
                self.store.set_file_path(session_id, str(combined))
                self.store.set_slot(session_id, "sides", "double", source="inferred")
                self.store.record_event(
                    session_id, "back_side_merged", {"front": prev_file, "back": str(dest)}
                )
                merged_back = True
        if not merged_back:
            self.store.set_file_path(session_id, str(dest))

        if State(row.state) == State.INTAKE:
            self.store.transition(session_id, State.CLASSIFY, "file_first")
        row = self._get(session_id)

        # 상품 미정이면 파일 재단 규격으로 상품을 알아챈다 (90x50 → 명함처럼 당연한 추론)
        detected = None
        if not row.product:
            detected = self._infer_product_from_file(dest)
            if detected:
                self.store.set_product(session_id, detected)
                self.store.record_event(session_id, "product_inferred_from_file", {"product": detected})
                row = self._get(session_id)
                if State(row.state) == State.CLASSIFY:
                    self.store.transition(session_id, State.SLOT_FILLING, "product_from_file")
                row = self._get(session_id)

        # 파일의 실제 크기를 주문 규격으로 삼는다 (검판 전에 반영 → 규격 불일치 헛질문 방지)
        size_from_file = self._apply_file_size(session_id)
        row = self._get(session_id)

        # 정식 검판 전이는 SLOT_FILLING/PROOF_CONFIRM에서만 가능 (상품 미정이면 비공식 검판)
        formal = State(row.state) in (State.SLOT_FILLING, State.PROOF_CONFIRM)
        if formal:
            self.store.transition(session_id, State.FILE_CHECK, "file_uploaded")

        report = self._run_preflight(session_id)

        if formal:
            self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")

        notices = [] if row.product else ["file_received_need_product"]
        if merged_back:
            notices.insert(0, "back_side_merged")
        if size_from_file:
            notices.insert(0, size_from_file)  # 이미 완성된 코드 (size_from_file:.. 또는 size_snapped:..)
        if image_wrapped:
            notices.insert(0, "image_intake")  # 이미지 접수 — 벡터 검수는 해당 없음 (안내)
        result = self._advance(session_id, notices=notices, kind="upload", report=report)
        if detected:
            result.directives.detected_product = self.catalog[detected].display_name
        return result

    def _apply_file_size(self, session_id: str) -> str | None:
        """업로드한 파일의 실제 재단 크기를 '주문 규격'으로 삼는다.

        파일이 곧 인쇄할 크기다 — 미리 고른 값과 파일이 다르면 파일이 이긴다
        (그래야 '파일이 주문과 달라요, 어느 쪽?' 같은 헛질문이 안 생긴다).
        표준 규격에 맞으면 그 규격으로, 아니면 파일 실측값(예: 53x94)을 그대로 쓴다.
        반환: 이전 값(사용자·기본값)을 파일 기준으로 바꿨으면 새 크기 문자열, 아니면 None.
        """
        from core.preflight.engine import CheckContext

        row = self._get(session_id)
        if not row.product or not row.file_path:
            return None
        schema = self.catalog[row.product]
        if "size" not in schema.slots:
            return None
        try:
            ctx = CheckContext(row.file_path)
            size = ctx.trim_size_mm(0)
            if not size:
                # TrimBox 없는 파일도 흔하다 — MediaBox 크기로 규격을 잡는다
                from core.preflight.engine import pt_to_mm

                mb = ctx.page_boxes(0).get("media")
                if mb:
                    size = (pt_to_mm(mb[2] - mb[0]), pt_to_mm(mb[3] - mb[1]))
            ctx.close()
        except Exception:
            return None
        if not size:
            return None
        w, h = size
        match = match_size_choice(schema, w, h)  # 정확 일치 (±1mm)
        snapped = None
        if match is None:
            # 규격 + 재단여백 범위면 그 표준 규격으로 스냅 (예: 53x94 → 90x50 명함).
            # 인쇄 파일은 재단선 밖 여백을 두는 게 정상이라, 몇 mm 큰 건 '문제'가 아니다.
            snapped = self._bleed_tolerant_match(schema, w, h)
        value = match or snapped or f"{round(w)}x{round(h)}"
        entry = (row.slots or {}).get("size", {})
        prev = entry.get("value")
        if prev == value:
            return None
        self.store.set_slot(session_id, "size", value, source="file")
        if snapped is not None:
            # 파일이 표준규격 + 재단여백 → 그 표준규격으로 맞췄다고 한 번 알린다
            return f"size_snapped:{round(w)}x{round(h)}:{value}"
        # 이전에 사람이 고른 값이 있었는데 파일이 다르면 알려준다 (조용히 바꾸지 않음)
        if prev is not None and entry.get("source") in ("user", "default"):
            return f"size_from_file:{value}"
        return None

    def _option_prices(self, row: OrderSession, schema: ProductSchema) -> dict[str, Any]:
        """옵션(용지·코팅·사이즈 등)을 하나씩 바꿔가며 실제 견적을 계산한 표.

        수량이 정해져 있어야 계산할 수 있다. 각 슬롯의 선택지별 총액(부가세 포함)을 담아,
        고객이 "각각 얼마?", "옵션별로 비교해줘" 같은 걸 물으면 LLM이 지어내지 않고
        이 값으로 답하게 한다. (측정·계산은 결정론, LLM은 번역만 — 철칙 2)
        """
        if not row.product:
            return {}
        slots = row.slots or {}
        base = {k: v.get("value") for k, v in slots.items() if v.get("value") is not None}
        # 필수값을 기본값으로 채워 계산 가능한 상태로 (비교축은 아래에서 덮어씀)
        for name, sdef in schema.required_slots().items():
            if base.get(name) is None:
                if sdef.has_default:
                    base[name] = sdef.default
                elif sdef.choices:
                    base[name] = str(sdef.choices[0])
        if base.get("quantity") is None:
            return {}  # 수량 없이는 가격을 낼 수 없다

        matrix: dict[str, Any] = {}
        for name, sdef in schema.slots.items():
            choices = list(sdef.choices)
            if len(choices) < 2:
                continue  # 고를 게 하나뿐이면 비교 의미 없음
            priced: list[dict[str, Any]] = []
            for choice in choices:
                q = quote(row.product, {**base, name: str(choice)})
                if q is None or q.missing:
                    continue
                priced.append({"value": str(choice), "total": q.total})
            if len(priced) >= 2:
                matrix[name] = {
                    "display_name": sdef.display_name or name,
                    "unit": sdef.unit,
                    "quantity": base["quantity"],
                    "current": base.get(name),
                    "choices": priced,
                }
        return matrix

    def _bleed_tolerant_match(self, schema: ProductSchema, w: float, h: float) -> str | None:
        """파일이 '표준 규격 + 재단여백' 범위 안이면 그 표준 규격 선택지를 돌려준다.

        규격보다 큰 쪽은 넉넉히(재단여백 ≈4mm/변까지), 작은 쪽은 반올림 오차만 허용.
        여러 규격이 걸리면 가장 가까운 것. (page_size 체크의 허용치와 같은 취지)
        """
        size_slot = schema.slots.get("size")
        if not size_slot or not size_slot.choices:
            return None
        over, under = 8.0, 1.0

        def fits(dim: float, target: float) -> bool:
            return (target - under) <= dim <= (target + over)

        best: tuple[float, str] | None = None
        for choice in size_slot.choices:
            cm = choice_to_mm(str(choice))
            if not cm:
                continue
            cw, ch = cm
            if not ((fits(w, cw) and fits(h, ch)) or (fits(w, ch) and fits(h, cw))):
                continue
            d = min(abs(w - cw) + abs(h - ch), abs(w - ch) + abs(h - cw))
            if best is None or d < best[0]:
                best = (d, str(choice))
        return best[1] if best else None

    def _infer_product_from_file(self, file_path: Path) -> str | None:
        """파일을 보고 '무엇을 만들려는지' 추정한다 (정확 매칭이 아니라 넉넉한 의도 추정).

        재단 크기를 카탈로그 규격들과 **비례 거리**로 견줘 가장 가까운 상품을 고른다.
        칼선(별색)이 있으면 스티커/라벨 쪽으로 가중한다. 어느 상품과도 많이 멀면 보류(질문).
        이건 '제안'이며, 틀리면 고객이 상품명을 말해 바로 바꾼다(오케스트레이터가 override).
        """
        from core.preflight.engine import CheckContext

        try:
            ctx = CheckContext(file_path)
            size = ctx.trim_size_mm(0)
            has_dieline = self._file_has_dieline(ctx)
            ctx.close()
        except Exception:
            return None
        if not size:
            return None
        return self._guess_product(size[0], size[1], has_dieline)

    def _file_has_dieline(self, ctx) -> bool:
        """콘텐츠에서 칼선(별색 Separation) 스트로크 존재 여부 — 스티커/라벨 신호."""
        try:
            from core.preflight.contentstream import VectorStroke

            for ev in ctx.content_events(0):
                if isinstance(ev, VectorStroke) and ev.color.space.startswith("Separation"):
                    return True
        except Exception:
            pass
        return False

    def _guess_product(self, w_mm: float, h_mm: float, has_dieline: bool) -> str | None:
        """재단 크기(+칼선)로 가장 그럴듯한 상품을 추정. 15% 이내로 가까우면 그 상품."""
        best: tuple[float, str] | None = None  # (거리, 상품)
        for pid, schema in self.catalog.items():
            size_slot = schema.slots.get("size")
            if not size_slot:
                continue
            for choice in size_slot.choices:
                target = choice_to_mm(str(choice))
                if not target:
                    continue
                tw, th = target
                # 두 방향(가로/세로 교환) 중 가까운 쪽의 비례 거리
                d = min(
                    abs(w_mm - tw) / tw + abs(h_mm - th) / th,
                    abs(w_mm - th) / th + abs(h_mm - tw) / tw,
                )
                # 칼선 상품(스티커·라벨)엔 칼선 유무를 반영해 가중
                if pid in ("sticker", "label"):
                    d = d * (0.7 if has_dieline else 1.4)
                elif has_dieline:
                    d = d * 1.4  # 칼선 있는데 낱장 상품이면 덜 그럴듯
                if best is None or d < best[0]:
                    best = (d, pid)
        if best is None:
            return None
        return best[1] if best[0] <= 0.30 else None

    def _page_count(self, path: str | Path) -> int | None:
        import pikepdf

        try:
            with pikepdf.open(path) as pdf:
                return len(pdf.pages)
        except Exception:
            return None

    def _merge_front_back(self, front: Path, back: Path, out: Path) -> bool:
        """앞면 PDF + 뒷면 PDF → 앞(1p)·뒤(2p) 2페이지로 합친다 (양면 접수)."""
        import pikepdf

        try:
            pdf = pikepdf.open(front)
            with pikepdf.open(back) as b:
                for page in b.pages:
                    pdf.pages.append(page)
            pdf.save(out)
            pdf.close()
            return True
        except Exception:
            return False

    def _reclassify_reset(self, session_id: str) -> None:
        """상품이 바뀌었을 때: 이전 상품용 사양 슬롯을 비우고, 파일이 있으면 새 상품으로 재검판."""
        self.store.clear_slots(session_id)
        row = self._get(session_id)
        if row.file_path and State(row.state) in (State.SLOT_FILLING, State.PROOF_CONFIRM):
            if State(row.state) == State.PROOF_CONFIRM:
                self.store.transition(session_id, State.SLOT_FILLING, "product_changed")
            self.store.transition(session_id, State.FILE_CHECK, "product_changed_recheck")
            self._run_preflight(session_id)
            self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")

    def handle_autofix(self, session_id: str, check_id: str) -> TurnResult:
        """autofix 적용 (도련 연장 / RGB→CMYK 변환). 고친 파일로 교체 후 재검판.

        어떤 보정을 할지는 체크가 리포트에 담은 fix_id로 정한다 (extend_bleed / to_cmyk).
        """
        row = self._get(session_id)
        if not row.file_path:
            return self._advance(session_id, notices=["autofix_no_file"])

        # 최신 리포트에서 이 항목의 fix_id를 찾는다
        report0 = self._latest_report(session_id)
        cr = report0.by_id(check_id) if report0 else None
        fix_id = cr.autofix.fix_id if (cr and cr.autofix.available) else None
        if not fix_id:
            return self._advance(session_id, notices=[f"autofix_unsupported:{check_id}"])

        src = Path(row.file_path)
        fixed = src.parent / f"{src.stem}_fixed.pdf"
        pv_dir = PREVIEW_DIR / session_id
        if fix_id == "to_cmyk":
            from core.autofix.to_cmyk import to_cmyk

            fix_result = to_cmyk(src, fixed, preview_dir=pv_dir)
        else:  # extend_bleed (기본)
            # 품목별 요구 도련만큼 보정 (명함 1mm / 전단 2mm / 스티커 3mm)
            from core.preflight.adsland_guide import DEFAULT_RULE, rule_for

            bleed_mm = rule_for(row.product).bleed_mm or DEFAULT_RULE.bleed_mm
            fix_result = extend_bleed(src, fixed, bleed_mm=bleed_mm, preview_dir=pv_dir)
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
        sides: str | None = None,
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
        # 인쇄면 지정이 들어오면 슬롯에 반영 → 생성 시 앞/뒤 페이지 수가 맞춰진다
        if sides in ("single", "double"):
            self.store.set_slot(session_id, "sides", sides, source="user")

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
        row = self._get(session_id)
        # 인쇄면 슬롯이 양면이면 뒷면까지 2페이지로 생성 (page_count·견적이 자동으로 맞춰짐)
        sides = (row.slots or {}).get("sides", {}).get("value")
        double_sided = sides == "double"
        out_dir = UPLOAD_DIR / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "design_namecard.pdf"
        info = generate_namecard(content, out, template=template, double_sided=double_sided)
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

    def _render_preview_png(
        self,
        pdf_path: Path,
        session_id: str,
        scale: float = 2.5,
        page: int = 0,
        orient_landscape: bool = False,
    ) -> str | None:
        """PDF 한 페이지 → 미리보기 PNG (pdfium). previews 디렉터리에 저장.

        orient_landscape: 세로로 렌더된 페이지(명함 등 가로 규격인데 파일이 세로)는 90° 돌려
        가로로 맞춘다 — 3D·평면 어디서든 규격 방향(90×50)대로 보이게. (표시용, 원본 PDF는 그대로)
        """
        try:
            import pypdfium2 as pdfium
            from PIL import Image

            out_dir = PREVIEW_DIR / session_id
            out_dir.mkdir(parents=True, exist_ok=True)
            suffix = f"_p{page}" if page else ""
            out = out_dir / f"{pdf_path.stem}{suffix}_preview.png"
            doc = pdfium.PdfDocument(str(pdf_path))
            try:
                if page >= len(doc):
                    return None
                img = doc[page].render(scale=scale).to_pil()
            finally:
                doc.close()
            if orient_landscape and img.height > img.width:
                # 세로 파일 → 가로 규격에 맞춰 90° 회전 (반시계)
                img = img.transpose(Image.Transpose.ROTATE_90)
            img.save(out)
            return str(out)
        except Exception:
            return None

    def _card_previews(self, row: OrderSession, session_id: str) -> tuple[str | None, str | None]:
        """확정 카드·최종 확인 3D용 앞/뒷면 미리보기. 명함은 가로 규격에 맞춰 회전."""
        if not row.file_path:
            return None, None
        landscape = row.product == "namecard"
        front = self._render_preview_png(
            Path(row.file_path), session_id, page=0, orient_landscape=landscape
        )
        back = None
        try:
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument(str(row.file_path))
            n_pages = len(doc)
            doc.close()
        except Exception:
            n_pages = 1
        if n_pages >= 2:
            back = self._render_preview_png(
                Path(row.file_path), session_id, page=1, orient_landscape=landscape
            )
        return front, back

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
        changes = self._build_changes(session_id)
        front_preview, back_preview = self._card_previews(row, session_id)
        d = ReplyDirectives(kind="confirm", order_no=order_no, quote=quote_result, changes=changes)
        cards = [
            {
                # 발주·생산 인계 명세 — 무엇을 받는지 명확히
                "type": "order_confirmed",
                "product": row.product,
                "order_no": order_no,
                "summary": {
                    "product": row.product,
                    "slots": {k: v.get("value") for k, v in (row.slots or {}).items()},
                    "total": quote_result.total if quote_result else None,
                },
                "final_preview": front_preview,        # 최종 확정본 앞면 (발주 인계)
                "back_preview": back_preview,          # 뒷면 (양면일 때)
                "changes": changes,                    # 접수본 대비 변경 내역
                "file_name": Path(row.file_path).name if row.file_path else None,
            }
        ]
        if changes:
            cards.append(
                {
                    "type": "change_summary",
                    "product": row.product,
                    "items": changes,
                    "original_preview": changes[0].get("before_preview"),
                    "final_preview": changes[-1].get("after_preview"),
                }
            )
        return TurnResult(session=self._view(row), directives=d, cards=cards)

    def view_session(self, session_id: str) -> SessionView:
        return self._view(self._get(session_id))

    def transcript(self, session_id: str) -> list[dict]:
        """이벤트 로그 (감사·디버그·데모 타임라인용)."""
        return [
            {"seq": e.seq, "ts": e.ts.isoformat(), "type": e.type, "payload": e.payload}
            for e in self.store.events(session_id)
        ]

    def order_sheet(self, session_id: str) -> dict:
        """고객과의 대화 → 내부 검수자·생산에게 전달되는 '오더지'(작업지시서).

        대화에서 확정된 사양·검판 결과·변경 내역·최종 파일을 한 장으로 정리한다.
        결정론 엔진의 출력만 담으며, 검수자가 이걸 보고 바로 생산에 넘길 수 있어야 한다.
        """
        row = self._get(session_id)
        schema = self.catalog.get(row.product) if row.product else None
        report = self._latest_report(session_id)
        quote = None
        if schema:
            quote = self._quote(row) or self._estimate_quote(row, schema)
        changes = self._build_changes(session_id)

        order_no = None
        created_at = None
        for e in self.store.events(session_id):
            if e.type == "session_created":
                created_at = e.ts.isoformat()
            if e.type == "payment_mock":
                order_no = e.payload.get("order_no")

        # 사양 (스키마 선언 순서대로)
        specs: list[dict] = []
        if schema:
            for name, sd in schema.slots.items():
                v = (row.slots or {}).get(name, {}).get("value")
                if v is None:
                    continue
                src = (row.slots or {}).get(name, {}).get("source")
                specs.append(
                    {"slot": name, "label": sd.display_name or name, "value": v,
                     "unit": sd.unit, "source": src}
                )

        # 검판 요약 (문제 항목 위주)
        from core.llm.roles import translate_check

        issues: list[dict] = []
        if report is not None:
            for r in report.results:
                if str(r.status) == "pass":
                    continue
                issues.append({"check_id": r.check_id, "status": str(r.status),
                               "message": translate_check(r)})

        final_preview = (
            self._render_preview_png(Path(row.file_path), session_id) if row.file_path else None
        )
        content = {k: v for k, v in (row.card_content or {}).items() if v}

        return {
            "session_id": session_id,
            "order_no": order_no,
            "status": row.state,
            "confirmed": bool(row.customer_confirmed),
            "escalated": bool(row.escalated),
            "escalation_reasons": list(row.escalation_reasons or []),
            "created_at": created_at,
            "product": row.product,
            "specs": specs,
            "card_content": content,
            "quote": quote.model_dump(mode="json") if quote else None,
            "quote_is_estimate": bool(quote and self._quote(row) is None),
            "file": {
                "name": re.sub(r"^up_\d+_", "", Path(row.file_path).name) if row.file_path else None,
                "pages": self._page_count(row.file_path) if row.file_path else None,
                "preview": final_preview,
            },
            "preflight": {
                "gate_ok": report.gate_ok if report else None,
                "issues": issues,
            },
            "changes": changes,
            "turn_count": row.turn_count,
        }

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
        cut_type = (row.slots or {}).get("cut_type", {}).get("value")
        # 애즈랜드 가이드 기반 품목별 임계값 (도련·안전여백·총잉크량·선굵기)
        from core.preflight.adsland_guide import rule_for, safety_mm_for

        gr = rule_for(row.product)
        return OrderContext(
            product=row.product,
            size_mm=size_mm,
            page_count=self._expected_pages(row) if row.product else None,
            cut_type=cut_type,
            bleed_mm=gr.bleed_mm,
            safety_mm=safety_mm_for(row.product, cut_type),
            max_ink_percent=gr.max_ink_percent,
            min_line_pt=gr.min_line_pt,
            has_cutline=self._has_cutline(row.id),
        )

    def _has_cutline(self, session_id: str) -> bool:
        """도무송 칼선 파일을 별도로 받아 검증 통과했는가 (이벤트 기록으로 판단)."""
        try:
            return any(e.type == "cutline_accepted" for e in self.store.events(session_id))
        except Exception:
            return False

    def flip_back_side(self, session_id: str) -> TurnResult:
        """양면 파일의 뒷면(2페이지)을 180° 돌린다 — 뒷면 위아래가 뒤집혀 보일 때."""
        import pikepdf

        row = self._get(session_id)
        if not row.file_path or self._page_count(row.file_path) != 2:
            return self._advance(session_id, notices=["flip_needs_double"], kind="turn")
        src = Path(row.file_path)
        out = src.parent / f"{src.stem}_flip.pdf"
        try:
            pdf = pikepdf.open(src)
            pg = pdf.pages[1]
            cur = int(pg.get("/Rotate", 0)) if "/Rotate" in pg else 0
            pg.Rotate = (cur + 180) % 360
            pdf.save(out)
            pdf.close()
        except Exception:
            return self._advance(session_id, notices=["flip_failed"], kind="turn")
        self.store.set_file_path(session_id, str(out))
        self.store.record_event(session_id, "back_flipped", {})
        if State(row.state) in (State.SLOT_FILLING, State.PROOF_CONFIRM):
            self.store.transition(session_id, State.FILE_CHECK, "back_flip_recheck")
            report = self._run_preflight(session_id)
            self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")
        else:
            report = self._run_preflight(session_id)
        return self._advance(session_id, notices=["back_flipped"], kind="autofix", report=report)

    def handle_cutline(self, session_id: str, src_path: str | Path, original_name: str = "") -> TurnResult:
        """도무송 칼선 파일 접수 → 검증 → 통과 시 칼선 제공됨 기록 후 재검판."""
        from core.preflight.cutline import validate_cutline

        row = self._get(session_id)
        src = Path(src_path)
        # 이미지 칼선도 PDF로 감싼다
        try:
            head = src.read_bytes()[:16]
        except Exception:
            head = b""
        from core.intake.image_to_pdf import is_image_bytes

        if is_image_bytes(head):
            from core.intake.image_to_pdf import image_to_pdf

            pdf_path = src.with_name(src.stem + "_cutimg.pdf")
            try:
                image_to_pdf(src, pdf_path)
                src = pdf_path
            except Exception:
                pass

        v = validate_cutline(src)
        self.store.record_event(session_id, "cutline_uploaded", {"path": str(src), **v})
        if not v["ok"]:
            # 검증 실패 — 사유를 notice로 안내 (재업로드 유도)
            notices = [f"cutline_invalid:{r}" for r in v["reasons"]] or ["cutline_invalid"]
            return self._advance(session_id, notices=notices, kind="upload")

        self.store.record_event(session_id, "cutline_accepted", {"path": str(src), "size_mm": v["size_mm"]})
        # 재검판 (dieline이 has_cutline으로 통과되게)
        if State(row.state) in (State.SLOT_FILLING, State.PROOF_CONFIRM):
            self.store.transition(session_id, State.FILE_CHECK, "cutline_recheck")
            report = self._run_preflight(session_id)
            self.store.transition(session_id, State.SLOT_FILLING, "preflight_done")
        else:
            report = self._run_preflight(session_id)
        return self._advance(session_id, notices=["cutline_accepted"], kind="upload", report=report)

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

    def _estimate_quote(self, row: OrderSession, schema: ProductSchema) -> QuoteResult | None:
        """확정 견적이 아직 안 될 때 '예상 견적'. 수량만 있으면 나머지 필수값을
        기본값(없으면 첫 선택지)으로 채워 예상가를 낸다 — 고객이 바로 가격 감을 잡게."""
        if not row.product:
            return None
        slots = row.slots or {}
        values = {k: v.get("value") for k, v in slots.items() if v.get("value") is not None}
        if values.get("quantity") is None:
            return None  # 수량 없이는 가격을 낼 수 없다
        for name, sd in schema.required_slots().items():
            if values.get(name) is None:
                if sd.has_default:
                    values[name] = sd.default
                elif sd.choices:
                    values[name] = str(sd.choices[0])
                else:
                    return None
        q = quote(row.product, values)
        if q is not None and not q.missing:
            return q
        # 크기가 표준 규격에 없으면(맞춤 규격 파일 등) 가장 가까운 규격으로 예상가를 낸다.
        # → 대화가 "규격 골라주세요"로 막히지 않고 대략적 가격이라도 나온다.
        near = self._nearest_size_choice(schema, values.get("size"))
        if near and near != values.get("size"):
            values2 = dict(values)
            values2["size"] = near
            q2 = quote(row.product, values2)
            if q2 is not None and not q2.missing:
                return q2
        return None

    def _nearest_size_choice(self, schema: ProductSchema, size_value) -> str | None:
        """주어진 크기에 가장 가까운 카탈로그 사이즈 선택지 (비례 거리 기준)."""
        size_def = schema.slots.get("size")
        if not size_def or not size_def.choices or not size_value:
            return None
        target = choice_to_mm(str(size_value))
        if not target:
            return None
        tw, th = target
        best: tuple[float, str] | None = None
        for choice in size_def.choices:
            cm = choice_to_mm(str(choice))
            if not cm:
                continue
            cw, ch = cm
            d = min(
                abs(tw - cw) / cw + abs(th - ch) / ch,
                abs(tw - ch) / ch + abs(th - cw) / cw,
            )
            if best is None or d < best[0]:
                best = (d, str(choice))
        return best[1] if best else None

    def _build_changes(self, session_id: str) -> list[dict]:
        """이벤트 로그에서 '접수본 → 최종본' 변경 항목을 뽑는다 (검판원·고객 공용).

        지금은 자동 보정(autofix)이 유일한 변경원 — 파일이 이렇게 들어와 이렇게 바뀌었다를
        전/후 미리보기와 함께 기록한다. 본개발에서 색공간 변환 등으로 확장.
        """
        changes: list[dict] = []
        for e in self.store.events(session_id):
            if e.type != "autofix_applied":
                continue
            p = e.payload or {}
            previews = p.get("previews") or [{}]
            pv = previews[0] if previews else {}
            cid = p.get("check_id")
            label = "재단 여백 자동 연장" if cid == "bleed" else f"자동 보정({cid})"
            changes.append(
                {
                    "kind": "autofix",
                    "check_id": cid,
                    "label": label,
                    "before": "재단 여백 없음(0mm)" if cid == "bleed" else "보정 전",
                    "after": f"사방 {round(float(p.get('bleed_mm', 3)))}mm 확보" if cid == "bleed" else "보정 후",
                    "before_preview": pv.get("before"),
                    "after_preview": pv.get("after"),
                }
            )
        return changes

    def _advance(
        self,
        session_id: str,
        notices: list[str] | None = None,
        negative_sentiment: bool = False,
        kind: str = "turn",
        report: PreflightReport | None = None,
        customer_text: str = "",
    ) -> TurnResult:
        """턴 마무리 공통 파이프라인: 추론 반영 → 질문 정책 → 견적 → 시그널 → 전이 → 지시서."""
        row = self._get(session_id)
        d = ReplyDirectives(kind=kind, notices=list(notices or []), customer_message=customer_text)

        if report is None:
            report = self._latest_report(session_id)
        # 리포트는 업로드·보정·시안생성 직후에만 응답에 서술한다 (매 턴 검판 말 반복 방지).
        # 게이트·추론 등 내부 판단은 아래 지역변수 report를 그대로 쓴다.
        if kind in ("upload", "autofix", "design"):
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

        # 고객이 "용지 뭐 있어?"처럼 옵션을 물으면, 그 슬롯을 버튼으로 띄운다 (사람이 고르게).
        # 기본값이 있어 평소엔 질문 안 하는 슬롯도 이때는 선택지를 보여준다.
        asked = _asked_options_slot(customer_text, schema)
        if asked:
            d.asked_slot = asked
            if not any(q.slot == asked for q in d.questions):
                sdef = schema.slots[asked]
                opts = list(sdef.quick_options) or list(sdef.choices)
                d.questions.insert(
                    0,
                    SlotQuestion(
                        slot=asked,
                        display_name=sdef.display_name or asked,
                        reason="customer_asked_options",
                        quick_options=list(sdef.quick_options),
                        options=opts,
                        allow_other=True,
                    ),
                )

        # 자동 채움 적용 + 통보
        for af in decision.auto_filled:
            self.store.set_slot(session_id, af.slot, af.value, source="default")
        d.auto_filled = decision.auto_filled
        row = self._get(session_id)

        # 뒷면(양면) 확인 — 뒷면이 흔한 상품(명함·전단·엽서·포토카드)에서 앞면 1장만 올렸고
        # 사용자가 인쇄면을 안 정했으면, 인쇄사가 당연히 묻는 "뒷면 있으세요?"를 묻는다.
        sides_def = schema.slots.get("sides")
        if sides_def is not None and report is not None and row.file_path and not row.design_mode:
            pc = report.by_id("page_count")
            file_pages = (pc.measured or {}).get("file_pages") if pc else None
            sides_entry = (row.slots or {}).get("sides", {})
            if (
                file_pages == 1
                and sides_entry.get("source") != "user"
                and not any(q.slot == "sides" for q in d.questions)
            ):
                d.auto_filled = [af for af in d.auto_filled if af.slot != "sides"]
                d.questions.insert(
                    0,
                    SlotQuestion(
                        slot="sides",
                        display_name=sides_def.display_name or "인쇄면",
                        reason="confirm_back_side",
                        options=["single", "double"],  # UI가 단면/양면으로 라벨링
                        allow_other=False,
                    ),
                )
                d.offer_back_side = True

        # 양면이라 했는데 앞면 1장뿐이면 뒷면 파일을 요청한다 (뒷면 업로드 시 재검판)
        needs_back = False
        if sides_def is not None and report is not None and row.file_path:
            pc = report.by_id("page_count")
            file_pages = (pc.measured or {}).get("file_pages") if pc else None
            if (row.slots or {}).get("sides", {}).get("value") == "double" and file_pages == 1:
                needs_back = True
                if "need_back_side_file" not in d.notices:
                    d.notices.append("need_back_side_file")

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
                    # 맞춤 규격(비표준 size)은 custom_size_estimate가 따로 설명하므로
                    # "가격표에 없어요" 안내를 중복·반복하지 않는다.
                    size_def = schema.slots.get("size")
                    size_val = (row.slots or {}).get("size", {}).get("value")
                    custom_size = bool(
                        size_def and size_val and str(size_val) not in {str(c) for c in size_def.choices}
                    )
                    for m in quote_result.missing:
                        if m.startswith("size=") and custom_size:
                            continue
                        d.notices.append(f"quote_missing:{m}")
                    quote_result = None  # 조회 실패한 견적은 표시하지 않는다
                else:
                    d.quote = quote_result

        # 확정 견적이 아직 없으면 '예상 견적'이라도 보여준다 (결과 우선 — 가격 먼저)
        if d.quote is None:
            est = self._estimate_quote(row, schema)
            if est is not None:
                d.quote = est
                d.estimate = True
                # 파일 크기가 비표준이라 가까운 규격으로 예상가를 낸 경우 알려준다
                # (파일 관련 턴에만 — 매 턴 반복 방지)
                size_val = (row.slots or {}).get("size", {}).get("value")
                size_def = schema.slots.get("size")
                if (
                    kind in ("upload", "autofix", "design")
                    and size_def
                    and size_val
                    and str(size_val) not in {str(c) for c in size_def.choices}
                ):
                    near = self._nearest_size_choice(schema, size_val)
                    if near:
                        d.notices.append(f"custom_size_estimate:{size_val}:{near}")

        # 도무송인데 칼선이 없으면 칼선 파일을 따로 요청한다 (애즈랜드 4파일 분리 접수 방식)
        if report is not None:
            dl = report.by_id("dieline")
            cut_val = (row.slots or {}).get("cut_type", {}).get("value")
            if (
                dl is not None
                and str(dl.status) == "fail"
                and cut_val == "die_cut"
                and not self._has_cutline(session_id)
            ):
                d.request_cutline = True

        # 옵션별 실제 가격을 미리 계산해 둔다 — 고객이 "각각 얼마?"처럼 물으면
        # LLM이 이 값을 그대로 써서 답한다(지어내지 않게). 수량을 알아야 계산 가능.
        d.option_prices = self._option_prices(row, schema)

        # 변경 이력 (접수본 → 최종본) — 자동 보정 등이 적용됐으면 채워진다
        d.changes = self._build_changes(session_id)

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

        # autofix 제안: 업로드/보정 직후엔 자동보정 가능한 fail·warn 모두(도련 연장·색상 변환 등),
        # 그 외엔 '막고 있는' fail만 (매 턴 반복 방지).
        autofixable = (
            [
                r.check_id
                for r in report.results
                if r.status in (CheckStatus.FAIL, CheckStatus.WARN) and r.autofix.available
            ]
            if report is not None
            else []
        )
        blocking_autofixable = (
            [r.check_id for r in report.results if r.status == CheckStatus.FAIL and r.autofix.available]
            if report is not None
            else []
        )
        otherwise_ready = (
            not d.questions
            and not d.conflicts
            and not required_missing
            and not needs_back
            and quote_result is not None
            and not quote_result.missing
        )
        if kind in ("upload", "autofix"):
            d.offer_autofix = autofixable
        elif (
            blocking_autofixable
            and otherwise_ready
            and report is not None
            and not report.uncertains
            and not [r for r in report.failures if not r.autofix.available]
        ):
            d.offer_autofix = blocking_autofixable
            d.notices.append("autofix_to_finish")

        # 상태 전진: 질문·충돌 없음 ∧ 파일 검판 clean → PROOF_CONFIRM
        state = State(row.state)
        ready = (
            not d.questions
            and not d.conflicts
            and not required_missing
            and not needs_back
            and report is not None
            and report.gate_ok
            and quote_result is not None
            and not quote_result.missing
        )
        # 고객이 이번 턴에 '최종 견적/확인'을 요청했는가 (그때만 카드를 띄운다)
        wants_final = _wants_final_review(customer_text)
        if state == State.SLOT_FILLING:
            if ready:
                row = self.store.transition(session_id, State.PROOF_CONFIRM, "all_slots_and_checks_ok")
                d.awaiting_confirm = True
                if wants_final:
                    d.show_final = True
                    d.confirm_review = self._confirm_specs(row, schema)
                else:
                    d.offer_final_review = True  # 계약서 안 들이밀고 먼저 물어본다
            elif needs_back:
                d.request_file = True  # 양면인데 앞면만 → 뒷면 파일 요청
            elif not row.file_path:
                # 명함은 파일이 없으면 시안 생성을 제안한다 (파일 없는 초보 고객 유입)
                if row.product in DESIGNABLE_PRODUCTS and not row.design_mode:
                    d.offer_design = True
                else:
                    d.request_file = True
        elif state == State.PROOF_CONFIRM:
            if ready:
                d.awaiting_confirm = True
                if wants_final:
                    d.show_final = True
                    d.confirm_review = self._confirm_specs(row, schema)
                else:
                    d.offer_final_review = True  # 매 턴 견적·확인 카드를 다시 띄우지 않는다
            else:
                # 확정 단계에서 조건이 깨졌으면 슬롯 수집으로 복귀
                row = self.store.transition(session_id, State.SLOT_FILLING, "proof_conditions_broken")

        return TurnResult(session=self._view(row), directives=d, cards=self._cards(d, row))

    def _confirm_specs(self, row: OrderSession, schema: ProductSchema) -> list[dict]:
        """최종 확인용 사양 체크리스트 — 값은 고객 언어 라벨로. 확정 직전에 한 번 훑게 한다."""
        from core.llm.roles import label_value  # 스펙 값 → 고객 언어 (예: die_cut → 도무송)

        specs: list[dict] = []
        for name, sd in schema.slots.items():
            v = (row.slots or {}).get(name, {}).get("value")
            if v is None:
                continue
            unit = sd.unit or ""
            value_label = label_value(name, v)
            if unit and value_label == str(v):  # 숫자값이면 단위를 붙여 읽기 쉽게 (200장)
                value_label = f"{value_label}{unit}"
            specs.append({"slot": name, "label": sd.display_name or name, "value_label": value_label})
        return specs

    def _cards(self, d: ReplyDirectives, row: OrderSession) -> list[dict]:
        """directives → UI 카드 목록 (docs/API.md 계약)."""
        cards: list[dict] = []
        if d.report is not None and d.kind in ("upload", "autofix"):
            from core.llm.roles import translate_check  # 항목별 고객 언어 설명(카드용)
            from core.preflight.adsland_guide import guide_url, remediation_for

            results = []
            for r in d.report.results:
                rd = r.model_dump(mode="json")
                rd["message"] = translate_check(r)  # 상세 설명은 카드에 (챗은 결과 요약만)
                # 통과 못 한 항목엔 애즈랜드 가이드 근거 수정 안내를 붙인다 (왜·어떻게·가이드 링크)
                if str(r.status) != "pass":
                    rem = remediation_for(r.check_id)
                    if rem is not None:
                        rd["fix_guide"] = {
                            "rule": rem.rule,
                            "why": rem.why,
                            "how_to_fix": rem.how_to_fix,
                            "autofixable": rem.autofixable,
                            "guide_url": guide_url(rem.source),
                        }
                results.append(rd)
            # 통과했지만 가이드상 권장 사항(막지는 않음) — '참고' 안내로 별도 표시
            advisories: list[dict] = []
            if "image_intake" in d.notices:
                advisories.append({
                    "key": "image_intake",
                    "text": "이미지(JPG/PNG) 파일이라 해상도·크기만 검수했어요. 선 굵기·별색 칼선·"
                            "글꼴 같은 벡터 검수는 이미지엔 해당되지 않아요. 정밀 인쇄는 PDF를 권장해요.",
                    "guide_url": guide_url("pdf"),
                })
            fe = d.report.by_id("font_embed")
            if fe is not None and str(fe.status) == "pass" and (fe.measured or {}).get("not_outlined"):
                advisories.append({
                    "key": "outline",
                    "text": "글꼴이 아웃라인(윤곽선) 처리되지 않았어요. 임베드돼 있어 진행은 가능하지만, "
                            "애즈랜드는 아웃라인을 권장해요.",
                    "guide_url": guide_url("indesign"),
                })
            cards.append(
                {
                    "type": "preflight_report",
                    "results": results,
                    "gate_ok": d.report.gate_ok,
                    "advisories": advisories,
                }
            )
        # 파일을 눈에 보이게 바꿨으면(뒷면 뒤집기 등) 결과를 바로 3D로 보여준다 — 말로만 "확인해보세요" X
        if "back_flipped" in d.notices and row.product == "namecard" and row.file_path:
            fp, bp = self._card_previews(row, row.id)
            if fp:
                cards.append({
                    "type": "preview_3d",
                    "product": row.product,
                    "preview": fp,
                    "back_preview": bp,
                    "caption": "뒷면을 뒤집었어요 — 드래그해서 앞뒤 방향 확인해보세요",
                })
        # 견적 카드: 슬롯 채우는 중엔 예상가를 보여줘 결정을 돕되(도움), 사양이 다 된
        # 확정 단계(awaiting_confirm)에서는 고객이 '최종 견적 볼게요'라고 할 때만 띄운다
        # — 매 턴 계약서처럼 들이밀지 않게.
        if d.quote is not None and not d.quote.missing and (not d.awaiting_confirm or d.show_final):
            cards.append({"type": "quote", "estimate": d.estimate, **d.quote.model_dump(mode="json")})
        # 최종 확인 체크리스트 — 고객이 최종 견적을 요청한 턴에만 (confirm_review는 그때만 채워짐)
        if d.confirm_review:
            # 명함이면 도안 예시를 3D로 함께 보여준다 (앞/뒷면)
            front_preview = back_preview = None
            if row.product == "namecard" and row.file_path:
                front_preview, back_preview = self._card_previews(row, row.id)
            cards.append(
                {
                    "type": "confirm_review",
                    "product": row.product,
                    "specs": d.confirm_review,
                    "total": d.quote.total if (d.quote and not d.quote.missing) else None,
                    "estimate": d.estimate,
                    "preview": front_preview,
                    "back_preview": back_preview,
                }
            )
        # 변경 내역 카드는 보정 직후·최종 확정 때만 (매 턴 반복 노출 방지)
        if d.changes and (d.kind == "autofix" or d.awaiting_confirm):
            cards.append(
                {
                    "type": "change_summary",
                    "product": row.product,
                    "items": d.changes,
                    "original_preview": d.changes[0].get("before_preview"),
                    "final_preview": d.changes[-1].get("after_preview"),
                }
            )
        if d.escalation_reasons:
            cards.append({"type": "escalation", "reasons": d.escalation_reasons})
        if d.order_no:
            pass  # order_confirmed 카드는 confirm()에서 직접 구성
        return cards
