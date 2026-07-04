"""LLM 역할 3종 — 분류기 / 슬롯 파서 / 대화 생성기 (+ 규칙 기반 폴백).

adapter가 None이면(=ANTHROPIC_API_KEY 없음) 규칙 기반 폴백이 완전히 동일한
인터페이스로 동작한다 — 데모는 키 없이도 완주해야 한다 (PLAN §5 M4).

의존 방향 (ADR-001): 이 모듈은 core/orchestrator/* 를 런타임에 절대 임포트하지 않는다.
LLM/규칙은 '제안(pydantic 검증 통과분)'과 '문장'만 만들고, 적용은 오케스트레이터
(core/orchestrator/chat.py → service.py)가 한다. render_reply의 directives/view 인자는
오케스트레이터가 만들어 넘기는 값 객체이며 여기서는 속성을 읽기만 한다
(타입 힌트는 TYPE_CHECKING 전용 임포트 — 런타임 의존 없음).

철칙 2: 이 모듈은 아무것도 측정·계산하지 않는다. 리포트의 숫자를 한국어로 '번역'만 한다.
LLM 모드에서 스키마 검증 실패는 ParseError 그대로 올린다 — 재시도/폴백은 chat.py 몫.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.design.schema import CardContent
from core.llm.adapter import LLMAdapter
from core.llm.parsing import (
    ClassifyProposal,
    CustomerType,
    Intent,
    ParseError,
    SlotProposal,
    extract_json,
    validate_proposal,
)
from core.products.schema import ProductSchema, SlotDef

if TYPE_CHECKING:  # 런타임 임포트 금지 — 타입 힌트 전용 (ADR-001)
    from core.orchestrator.service import ReplyDirectives, SessionView

# ---------------------------------------------------------------- 프롬프트 로딩

_PROMPT_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=16)
def _prompt_text(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _load_prompt(name: str, **subs: str) -> str:
    """버전 파일명(prompts/*_v1.md)을 읽고 {{자리표시}}에 데이터를 주입한다."""
    text = _prompt_text(name)
    for key, value in subs.items():
        text = text.replace("{{" + key + "}}", value)
    return text


# ================================================================ 1) 분류기

#: 상품별 한국어 키워드 (display_name은 catalog에서 자동 추가 — 여긴 별칭만)
_PRODUCT_ALIASES: dict[str, tuple[str, ...]] = {
    "sticker": ("스티커", "씰지", "씰"),
    "namecard": ("명함",),
    "flyer": ("전단지", "전단", "리플렛", "리플릿", "찌라시"),
    "poster": ("포스터",),
    "label": ("라벨", "레이블"),
}

#: B유형: 파일은 있으나 수정·보완이 필요하다는 표현
_TYPE_B_RE = re.compile(r"수정|고쳐|고치|보완")
#: C유형: 시안·디자인을 새로 만들어 달라는 표현 (인접 매칭 — "디자인 파일 확인해주세요" 오탐 방지)
_TYPE_C_RE = re.compile(r"(?:시안|디자인)\s*(?:이|을|를|도|좀|은|는|부터)?\s*(?:새로\s*)?(?:만들|제작|해\s?주|해\s?줘|뽑|잡아|필요)")


def classify_input(
    text: str,
    catalog: dict[str, ProductSchema],
    adapter: LLMAdapter | None,
) -> ClassifyProposal:
    """첫 발화 → 고객 유형(A/B/C) + 상품 인식 제안.

    adapter가 있으면 소형 모델(role="classify")로, 없으면 규칙 폴백으로.
    LLM 출력이 스키마를 통과하지 못하면 ParseError가 그대로 올라간다.
    """
    if adapter is not None:
        products_spec = json.dumps(
            [
                {
                    "id": pid,
                    "display_name": schema.display_name,
                    "keywords": list(_PRODUCT_ALIASES.get(pid, ())),
                }
                for pid, schema in catalog.items()
            ],
            ensure_ascii=False,
        )
        system = _load_prompt("classify_v1.md", products=products_spec)
        raw = adapter.complete(
            system, [{"role": "user", "content": text}], role="classify", max_tokens=300
        )
        proposal = validate_proposal(raw, ClassifyProposal)
        assert isinstance(proposal, ClassifyProposal)
        return proposal
    return _rule_classify(text, catalog)


def _rule_classify(text: str, catalog: dict[str, ProductSchema]) -> ClassifyProposal:
    signals = ["rule_fallback"]

    # 상품: 발화에서 가장 먼저 나오는 키워드 승 (동률이면 긴 키워드 우선)
    best: tuple[int, int, str, str] | None = None  # (pos, -len, product, keyword)
    for pid, schema in catalog.items():
        keywords = set(_PRODUCT_ALIASES.get(pid, ())) | {schema.display_name}
        for kw in keywords:
            if not kw:
                continue
            pos = text.find(kw)
            if pos == -1:
                continue
            cand = (pos, -len(kw), pid, kw)
            if best is None or cand < best:
                best = cand
    product = best[2] if best else None
    if best:
        signals.append(f"product_keyword:{best[3]}")

    # 고객 유형: 수정 요청(B) > 시안 제작 요청(C) > 기본(A)
    if _TYPE_B_RE.search(text):
        ctype = CustomerType.B
        signals.append("customer_type:B:수정·보완 표현")
    elif _TYPE_C_RE.search(text):
        ctype = CustomerType.C
        signals.append("customer_type:C:시안·디자인 제작 요청")
    else:
        ctype = CustomerType.A
        signals.append("customer_type:A:기본값")

    return ClassifyProposal(customer_type=ctype, product=product, confidence_signals=signals)


# ================================================================ 2) 슬롯 파서

#: "90x90" 형태 사이즈 (x·X·×·* 허용)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)")
_QTY_UNITS = r"(?:매|장|개|부)"
#: "천 장"→1000, "5천 장"→5000, "만 장"→10000, "3만 개"→30000
_QTY_KOR_RE = re.compile(rf"(\d[\d,]*)?\s*(천|만)\s*{_QTY_UNITS}")
#: "500매", "1,000장"
_QTY_NUM_RE = re.compile(rf"(\d[\d,]*)\s*{_QTY_UNITS}")

_DENY_RE = re.compile(r"취소|안\s?할래|안\s?살래|그만할래|필요\s?없")
_CHANGE_RE = re.compile(r"바꿔|바꾸|변경|말고|대신")
_COMPLAINT_RE = re.compile(r"불만|짜증|화나|화가|최악|늦어|늦었|엉망|실망|답답|어이없")
_QUESTION_RE = re.compile(r"\?|얼마|언제|되나요|인가요|나요\s*$")
_CONFIRM_RE = re.compile(r"좋아요|좋습니다|확정|진행|맞아요|맞습니다|주문할게|주문할래|이대로|괜찮아요|오케이")
#: 단독 토큰으로만 인정 ("네모나게"의 '네' 오탐 방지)
_CONFIRM_TOKENS = frozenset({"네", "예", "넵", "넹", "응", "그래", "좋아", "ㅇㅋ"})


def parse_slots(
    text: str,
    schema: ProductSchema | None,
    adapter: LLMAdapter | None,
    awaiting_confirm: bool = False,
) -> SlotProposal:
    """발화 → 슬롯 값·의도 제안. schema는 상품 미정이면 None일 수 있다.

    adapter가 있으면 소형 모델(role="parse") + 스키마 데이터 주입 프롬프트로,
    없으면 규칙 폴백으로. 검증 실패는 ParseError로 올라간다 (재시도는 chat.py 몫).
    """
    if adapter is not None:
        slots_spec = json.dumps(
            {
                name: {
                    "display_name": sdef.display_name,
                    "required": sdef.required,
                    "choices": sdef.choices,
                    "synonyms": sdef.synonyms,
                    "quick_options": sdef.quick_options,
                    "unit": sdef.unit,
                }
                for name, sdef in (schema.slots if schema else {}).items()
            },
            ensure_ascii=False,
            default=str,
        )
        system = _load_prompt(
            "slots_v1.md",
            product=schema.display_name if schema else "(미정)",
            slots_spec=slots_spec,
            awaiting_confirm="true" if awaiting_confirm else "false",
        )
        raw = adapter.complete(
            system, [{"role": "user", "content": text}], role="parse", max_tokens=500
        )
        proposal = validate_proposal(raw, SlotProposal)
        assert isinstance(proposal, SlotProposal)
        return proposal
    return _rule_parse(text, schema, awaiting_confirm)


def _fmt_size_part(s: str) -> str:
    """'90' → '90', '90.50' → '90.5' (불필요한 소수점 제거)."""
    f = float(s)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _extract_size(text: str) -> tuple[str | None, str]:
    """(a) 사이즈 패턴을 먼저 추출하고, 남은 텍스트를 돌려준다 (수량 오인 방지)."""
    m = _SIZE_RE.search(text)
    if m is None:
        return None, text
    value = f"{_fmt_size_part(m.group(1))}x{_fmt_size_part(m.group(2))}"
    return value, text[: m.start()] + " " + text[m.end() :]


def _extract_quantity(text: str) -> int | None:
    """(b) 숫자+단위(매/장/개/부). '천 장'→1000, '만 장'→10000, 콤마 숫자 허용."""
    m = _QTY_KOR_RE.search(text)
    if m:
        base = int(m.group(1).replace(",", "")) if m.group(1) else 1
        mult = 1000 if m.group(2) == "천" else 10000
        return base * mult
    m = _QTY_NUM_RE.search(text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _match_slot_terms(text: str, schema: ProductSchema) -> tuple[dict[str, Any], list[str]]:
    """(c) 스키마의 synonyms 키·choices 리터럴이 발화에 있으면 해당 슬롯 값 제안.

    - 긴 키 우선 매칭: "코팅 없이"가 "코팅"보다, "무광 느낌"이 "무광"보다 먼저 잡히고,
      잡힌 구간은 마스킹되어 짧은 키가 겹쳐 매칭되지 않는다.
    - 같은 슬롯에 여러 표현이 매칭되면 **발화에서 뒤에 나온 표현이 승자**다
      ("도톰한 방수 스티커" → material=pvc_white). 이 규칙은 테스트로 고정돼 있다.
    """
    candidates: list[tuple[str, str, Any]] = []  # (키워드, 슬롯, 값)
    for name, sdef in schema.slots.items():
        for key, value in sdef.synonyms.items():
            candidates.append((str(key), name, value))
        for choice in sdef.choices:
            candidates.append((str(choice), name, choice))
    candidates.sort(key=lambda t: (-len(t[0]), t[0]))  # 긴 키 우선, 동률은 사전순 (결정론)

    buf = list(text)
    hits: list[tuple[int, str, Any, str]] = []  # (위치, 슬롯, 값, 키워드)
    for key, slot, value in candidates:
        if not key:
            continue
        while True:
            idx = "".join(buf).find(key)
            if idx == -1:
                break
            hits.append((idx, slot, value, key))
            buf[idx : idx + len(key)] = "\0" * len(key)  # 겹침 매칭 차단

    resolved: dict[str, Any] = {}
    picked: dict[str, str] = {}
    for _, slot, value, key in sorted(hits, key=lambda h: h[0]):  # 뒤(오른쪽) 표현이 덮어씀
        resolved[slot] = value
        picked[slot] = key
    signals = [f"slot_term:{slot}={picked[slot]!r}" for slot in resolved]
    return resolved, signals


def _rule_intent(text: str, awaiting_confirm: bool) -> tuple[Intent, bool]:
    """(d) 의도 판정. 부정 감정 플래그는 의도와 독립적으로 세운다."""
    negative = bool(_COMPLAINT_RE.search(text))
    tokens = {t.strip(".,!~…?") for t in text.split()}

    if _DENY_RE.search(text):
        return Intent.DENY, negative
    if _CHANGE_RE.search(text):
        return Intent.CHANGE, negative
    if awaiting_confirm and "?" not in text and (
        _CONFIRM_RE.search(text) or tokens & _CONFIRM_TOKENS
    ):
        return Intent.CONFIRM, negative
    if negative:
        return Intent.COMPLAINT, negative
    if _QUESTION_RE.search(text):
        return Intent.QUESTION, negative
    return Intent.PROVIDE_INFO, negative


def _rule_parse(text: str, schema: ProductSchema | None, awaiting_confirm: bool) -> SlotProposal:
    signals = ["rule_fallback"]
    slots: dict[str, Any] = {}

    size, rest = _extract_size(text)
    if size is not None and (schema is None or "size" in schema.slots):
        slots["size"] = size
        signals.append(f"size_pattern:{size}")

    qty = _extract_quantity(rest)
    if qty is not None and (schema is None or "quantity" in schema.slots):
        slots["quantity"] = qty
        signals.append(f"quantity:{qty}")

    if schema is not None:
        matched, term_signals = _match_slot_terms(text, schema)
        for name, value in matched.items():
            slots.setdefault(name, value)  # 명시적 숫자 패턴(size/quantity)이 우선
        signals.extend(term_signals)

    intent, negative = _rule_intent(text, awaiting_confirm)
    signals.append(f"intent:{intent.value}")
    return SlotProposal(
        intent=intent, slots=slots, negative_sentiment=negative, confidence_signals=signals
    )


# ================================================================ 2.5) 명함 내용 파서 (시안 생성 경로)

#: 라벨 → CardContent 필드. "회사이름: 피플즈리그" 같은 표기 입력을 흡수한다.
_CARD_LABEL_PATTERNS: list[tuple[str, str]] = [
    ("company", r"회사\s*(?:이름|명)?|상호|법인명|소속"),
    ("name", r"(?:사람\s*)?이름|성함"),
    ("tel", r"유선|대표\s*번호|사무실\s*(?:전화|번호)|팩스"),
    ("phone", r"(?:휴대폰|핸드폰|휴대전화)\s*(?:번호)?|(?:전화|연락처|폰|번호)"),
    ("title", r"직위|직책|직함"),
    ("department", r"부서|팀\s*명?"),
    ("email", r"이\s?메일|메일|e-?mail"),
    ("address", r"주소"),
    ("tagline", r"슬로건|문구"),
]

_PHONE_MOBILE_RE = re.compile(r"01[016789][\s\-.]?\d{3,4}[\s\-.]?\d{4}")
_PHONE_TEL_RE = re.compile(r"0\d{1,2}[\s\-.]?\d{3,4}[\s\-.]?\d{4}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_COMPANY_RE = re.compile(r"(?:[\w가-힣&.\- ]{1,20}\s*(?:주식회사|\(주\)|㈜))|(?:(?:주식회사|\(주\)|㈜)\s*[\w가-힣&.\-]{1,20})")
#: 값 뒤에 붙는 지시·잡문 절단 ("수석 연구원 이렇게 넣어서 만들어줘" → "수석 연구원")
_VALUE_STOP_RE = re.compile(
    r"\s*(?:이렇게|이대로|요렇게|위\s*내용|넣어서?|들어가게|들어간|으로\s*(?:만들|해|부탁)|로\s*(?:만들|해|부탁)|만들|제작|부탁|해\s?주|해\s?줘|주세요|할게|입니다|이에요|예요|이고|이며).*$"
)

#: 템플릿 선택 표현 → 템플릿 id
_TEMPLATE_WORDS: dict[str, str] = {
    "모던": "modern",
    "컬러 바": "modern",
    "클래식": "classic",
    "가운데": "classic",
    "미니멀": "minimal",
    "심플": "minimal",
    "깔끔한 걸": "minimal",
}


def extract_template(text: str) -> str | None:
    """발화에서 템플릿 선택 표현 추출 (시안 흐름에서만 호출할 것 — 일반 대화 오탐 방지)."""
    for word, tmpl in _TEMPLATE_WORDS.items():
        if word in text:
            return tmpl
    return None


def parse_card_content(text: str, adapter: LLMAdapter | None) -> CardContent:
    """발화 → 명함에 인쇄할 내용 추출. 없는 필드는 빈 문자열 (merge는 오케스트레이터가)."""
    if adapter is not None:
        system = _load_prompt("card_content_v1.md")
        raw = adapter.complete(
            system, [{"role": "user", "content": text}], role="parse", max_tokens=500
        )
        try:
            return CardContent.model_validate(extract_json(raw))
        except Exception as e:  # pydantic 오류 포함 전부 ParseError로 수렴
            raise ParseError(f"명함 내용 추출 실패: {e}") from e
    return _rule_card_content(text)


def _clean_value(raw: str) -> str:
    value = raw.strip().strip(" :：,;·~-\t\r\n")
    value = _VALUE_STOP_RE.sub("", value).strip(" ,.\r\n\t")
    return value[:40].strip()


#: 약한 구분(맨 공백)으로 잡힌 값의 첫 토큰이 조사로 끝나면 라벨 오탐으로 본다
_JOSA_HEAD_RE = re.compile(r"^\S{1,4}(?:을|를|은|는|도|의|에)\s")


def _rule_card_content(text: str) -> CardContent:
    """라벨 우선 + 패턴 보조. 라벨 값은 다음 라벨 직전까지로 자른다."""
    # 1) 라벨 위치 수집 — sep이 콜론/조사면 강한 매칭, 맨 공백이면 약한 매칭
    spans: list[tuple[int, int, str, bool]] = []  # (label_start, value_start, field, strong)
    for field, label_pat in _CARD_LABEL_PATTERNS:
        # 라벨 앞이 글자면 라벨이 아니다 ("주식회사"의 '회사', "회사이름"의 '이름' 오탐 차단)
        for m in re.finditer(rf"(?<![\w가-힣])(?:{label_pat})\s*(?P<sep>[:：]\s*|(?:은|는)\s+|\s+)", text):
            sep = m.group("sep")
            strong = (":" in sep) or ("：" in sep) or (sep.strip() in ("은", "는"))
            spans.append((m.start(), m.end(), field, strong))
    # 같은 시작점은 라벨이 긴 쪽 우선, 이후 겹치는 라벨("회사이름" 속 "이름")은 제거
    spans.sort(key=lambda s: (s[0], -s[1]))
    filtered: list[tuple[int, int, str, bool]] = []
    for span in spans:
        if filtered and span[0] < filtered[-1][1]:
            continue  # 직전 라벨 영역 안에서 시작 → 부분 라벨 오탐
        filtered.append(span)

    data: dict[str, str] = {}
    for i, (start, vstart, field, strong) in enumerate(filtered):
        vend = filtered[i + 1][0] if i + 1 < len(filtered) else len(text)
        value = _clean_value(text[vstart:vend])
        if not value or field in data:
            continue
        if not strong and _JOSA_HEAD_RE.match(value + " "):
            continue  # "회사 이름을 크게..." 같은 일반 문장 오탐 차단
        data[field] = value

    # 2) 형식이 확실한 값은 전체 텍스트에서 재추출 (라벨 오분류 교정)
    m = _EMAIL_RE.search(text)
    if m:
        data["email"] = m.group(0)
    m = _PHONE_MOBILE_RE.search(text)
    if m:
        data["phone"] = m.group(0)
    else:
        m = _PHONE_TEL_RE.search(data.get("phone", "") or text)
        if m and "phone" in data:
            data["tel"] = m.group(0)
            data.pop("phone", None)
    # 휴대폰 라벨 값이 유선번호였던 경우 등: 번호 형식이 아니면 버린다
    if "phone" in data and not (_PHONE_MOBILE_RE.search(data["phone"]) or _PHONE_TEL_RE.search(data["phone"])):
        data.pop("phone")
    if "company" not in data:
        m = _COMPANY_RE.search(text)
        if m:
            data["company"] = _clean_value(m.group(0))

    return CardContent(**data)


# ================================================================ 3) 대화 생성기

#: 스펙 값 → 고객 언어 표기 (카탈로그 choices와 1:1)
VALUE_LABELS: dict[str, str] = {
    # 용지 (스티커)
    "art_250": "아트지 250g",
    "art_300": "아트지 300g",
    "art_300_matte": "아트지 300g 무광",
    "pvc_white": "방수 PVC(백색)",
    # 용지 (명함·전단·포스터·라벨)
    "snow_250": "스노우지 250g",
    "art_230": "아트지 230g",
    "vannouveau_210": "반누보 210g",
    "art_100": "아트지 100g",
    "art_150": "아트지 150g",
    "snow_120": "스노우지 120g",
    "art_200": "아트지 200g",
    "snow_200": "스노우지 200g",
    "art_paper": "아트지 라벨",
    "yupo": "유포지(방수)",
    "clear_pet": "투명 PET",
    # 코팅
    "matte": "무광",
    "gloss": "유광",
    "none": "없음",
    # 재단 형태
    "die_cut": "도무송(모양대로 재단)",
    "square": "사각",
    "circle": "원형",
    # 인쇄면
    "single": "단면",
    "double": "양면",
}

#: 체크 id → 고객 언어 항목명 (PLAN §6 표와 1:1)
CHECK_NAMES: dict[str, str] = {
    "bleed": "재단 여백",
    "resolution": "이미지 해상도",
    "colorspace": "색상 모드",
    "font_embed": "폰트 포함(임베딩)",
    "trim_safety": "재단선 안전 여백",
    "ink_total": "총 잉크량",
    "black_type": "검정 표현",
    "page_size": "페이지 크기",
    "page_count": "페이지 수",
    "transparency": "투명 효과",
    "dieline": "칼선",
    "min_line": "최소 선 굵기",
}

#: 폴백용 슬롯 표시명 (schema가 없을 때)
_SLOT_DISPLAY_FALLBACK: dict[str, str] = {
    "size": "사이즈",
    "quantity": "수량",
    "material": "용지",
    "coating": "코팅",
    "cut_type": "재단 형태",
    "sides": "인쇄면",
}

# ------------------------------------------------ 한국어 조사 도우미

#: 숫자 끝자리의 종성 (0영,1일,2이,3삼,4사,5오,6육,7칠,8팔,9구) — ㄹ은 8로 표기
_DIGIT_JONG: dict[str, int] = {"0": 21, "1": 8, "2": 0, "3": 16, "4": 0, "5": 0, "6": 1, "7": 8, "8": 8, "9": 0}


def _final_jong(word: str) -> int | None:
    """마지막 유효 글자의 종성 코드. 0=받침 없음, 8=ㄹ, None=판별 불가(영문 등)."""
    for ch in reversed(str(word).strip()):
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            return (code - 0xAC00) % 28
        if ch.isdigit():
            return _DIGIT_JONG.get(ch)
        if ch in " )]}\"'":
            continue
        return None
    return None


def _ro(word: Any) -> str:
    """~(으)로: 받침 없음·ㄹ 받침 → '로', 그 외 → '으로', 판별 불가 → '(으)로'."""
    jong = _final_jong(str(word))
    if jong is None:
        return "(으)로"
    return "로" if jong in (0, 8) else "으로"


def _eun(word: Any) -> str:
    """~은/는. 판별 불가 → '은(는)'."""
    jong = _final_jong(str(word))
    if jong is None:
        return "은(는)"
    return "은" if jong else "는"


# ------------------------------------------------ 값 표기 도우미


def _fmt_num(v: Any) -> str:
    """3.0 → '3', 0.5 → '0.5' — 문장에 넣을 숫자 표기."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _won(v: Any) -> str:
    return f"{int(v):,}원"


def _label(value: Any) -> str:
    return VALUE_LABELS.get(str(value), str(value))


def _slot_display(name: str, schema: ProductSchema | None) -> str:
    if schema is not None:
        sdef = schema.slots.get(name)
        if sdef is not None and sdef.display_name:
            return sdef.display_name
    return _SLOT_DISPLAY_FALLBACK.get(name, name)


# ------------------------------------------------ 프리플라이트 리포트 번역
# 12개 체크 전부 pass/warn/fail/uncertain 문장을 준비한다 (특화 없는 조합은 generic).
# measured의 숫자를 반드시 문장에 넣는다 — LLM/템플릿은 번역가일 뿐, 숫자를 만들지 않는다.


def _t_bleed(r: Any, status: str) -> str | None:
    req = _fmt_num((r.required or {}).get("min_mm", 3))
    mm = (r.measured or {}).get("min_mm")
    if status == "pass":
        return f"재단 여백은 사방 {_fmt_num(mm)}mm로 충분해요." if mm is not None else "재단 여백은 문제 없어요."
    if status == "warn":
        return f"재단 여백이 {_fmt_num(mm)}mm로 아슬아슬해요 (권장 사방 {req}mm)."
    if status == "fail":
        line = (
            f"재단 여백이 사방 {req}mm 필요한데 지금 파일은 {_fmt_num(mm if mm is not None else 0)}mm예요. "
            "재단할 때 가장자리가 흰색으로 잘릴 수 있어요."
        )
        if getattr(getattr(r, "autofix", None), "available", False):
            line += " 자동으로 여백을 늘려드릴까요?"
        return line
    return "재단 여백을 판정하기 어려운 파일이에요. 확인이 필요해서 담당자 검토로 넘겼어요."


def _t_resolution(r: Any, status: str) -> str | None:
    dpi = (r.measured or {}).get("min_dpi")
    req = _fmt_num((r.required or {}).get("pass_dpi", 300))
    if status == "pass":
        if dpi is None:
            return f"이미지 해상도는 인쇄 기준({req}dpi)에 문제 없어요."
        return f"이미지 해상도는 최저 {_fmt_num(dpi)}dpi로 인쇄 기준({req}dpi)을 충족해요."
    if status == "warn":
        return (
            f"이미지가 {_fmt_num(dpi)}dpi라 인쇄 기준 {req}dpi보다 낮아요. "
            "그대로 진행하면 조금 흐릿하게 나올 수 있어요."
        )
    if status == "fail":
        return (
            f"이미지가 {_fmt_num(dpi)}dpi라 인쇄 기준 {req}dpi보다 많이 낮아요. "
            "그대로 진행하면 흐릿하게 나옵니다. 가능하면 해상도가 더 높은 원본으로 교체해 주세요."
        )
    return "이미지 해상도를 측정하지 못했어요. 확인이 필요해서 담당자 검토로 넘겼어요."


def _t_colorspace(r: Any, status: str) -> str | None:
    n = len((r.measured or {}).get("rgb_objects") or [])
    if status == "pass":
        return "색상은 인쇄용(CMYK)으로 잘 준비돼 있어요."
    if status == "warn":
        return (
            f"화면용 색상(RGB)이 {n}곳에서 발견됐어요. 인쇄용(CMYK)으로 변환해서 진행할 수 있는데, "
            "화면에서 보시던 색과 조금 달라질 수 있어요."
        )
    if status == "fail":
        return "인쇄에 쓸 수 없는 색상 구성이 있어요. 파일을 CMYK로 저장해 다시 올려주세요."
    return "색상 모드를 판정하기 어려운 요소가 있어요. 확인이 필요해서 담당자 검토로 넘겼어요."


def _t_font_embed(r: Any, status: str) -> str | None:
    names = (r.measured or {}).get("unembedded_used_fonts") or []
    if status == "pass":
        return "폰트는 모두 파일에 포함돼 있어요."
    if status == "fail":
        shown = ", ".join(f"'{n}'" for n in names[:3]) or "일부"
        return (
            f"{shown} 폰트({len(names)}종)가 파일에 포함돼 있지 않아요. "
            "이대로 인쇄하면 글자 모양이 바뀔 수 있어서, 폰트 포함(임베딩) 또는 아웃라인 처리한 파일이 필요해요."
        )
    if status == "warn":
        return "일부 폰트의 포함 상태에 주의가 필요해요."
    return "일부 폰트의 포함 여부를 확인할 수 없었어요. 담당자 검토로 넘겼어요."


def _t_trim_safety(r: Any, status: str) -> str | None:
    m = r.measured or {}
    req = _fmt_num((r.required or {}).get("safe_margin_mm", 3))
    if status == "pass":
        return "글자와 중요한 내용이 재단선에서 안전하게 떨어져 있어요."
    if status == "uncertain":
        violations = m.get("violations") or []
        if violations:
            worst = min(v.get("char_bbox_mm_from_trim", 0) for v in violations)
            n = m.get("violation_count", len(violations))
            return (
                f"글자 {n}건이 재단선 {req}mm 안전선 안쪽까지 들어와 있어요(재단선까지 {_fmt_num(worst)}mm). "
                "잘려도 되는 배경인지, 잘리면 안 되는 내용인지 확인이 필요해서 담당자 검토로 넘겼어요."
            )
        return "재단 기준 위치를 찾지 못해 안전 여백 확인을 담당자 검토로 넘겼어요."
    if status in ("warn", "fail"):
        return f"글자·중요 요소가 재단선 {req}mm 안전선을 침범했어요. 배치를 안쪽으로 조정해 주세요."
    return None


def _t_ink_total(r: Any, status: str) -> str | None:
    p = (r.measured or {}).get("max_ink_percent")
    req = _fmt_num((r.required or {}).get("max_percent", 300))
    if status == "pass":
        if p is None:
            return "잉크 사용량은 기준 안에 있어요."
        return f"잉크 사용량은 최대 {_fmt_num(p)}%로 기준({req}%) 안에 있어요."
    if status in ("warn", "fail"):
        return (
            f"잉크 사용량이 최대 {_fmt_num(p)}%로 기준 {req}%를 넘어요. "
            "건조가 늦거나 뒷묻음이 생길 수 있어서 진한 색 영역의 잉크량을 낮추는 걸 권해드려요."
        )
    return "잉크량을 계산하지 못했어요. 확인이 필요해서 담당자 검토로 넘겼어요."


def _t_black_type(r: Any, status: str) -> str | None:
    n = len((r.measured or {}).get("rich_black_texts") or [])
    if status == "pass":
        return "검정 글자는 인쇄에 적합한 먹1도로 되어 있어요."
    if status in ("warn", "fail"):
        return (
            f"검정 글자 일부({n}곳)가 4색 혼합 검정으로 되어 있어요. "
            "작은 글자는 인쇄가 번져 보일 수 있어서 먹1도(K100)로 바꾸는 걸 권해드려요."
        )
    return "검정 표현을 판정하지 못했어요. 담당자 검토로 넘겼어요."


def _t_page_size(r: Any, status: str) -> str | None:
    m = r.measured or {}
    fs, os_ = m.get("file_size_mm"), m.get("order_size_mm")
    if status == "pass":
        return "파일 크기가 주문 규격과 일치해요."
    if status == "fail" and fs and os_:
        return (
            f"파일 크기가 {_fmt_num(fs[0])}x{_fmt_num(fs[1])}mm인데 주문하신 규격은 "
            f"{_fmt_num(os_[0])}x{_fmt_num(os_[1])}mm예요. 어느 쪽 크기로 진행할지 알려주세요."
        )
    if status == "fail":
        return "파일 크기가 주문 규격과 달라요. 어느 쪽 크기로 진행할지 알려주세요."
    if status == "warn":
        return "파일 크기가 주문 규격과 미세하게 달라요."
    return "페이지 크기를 재지 못했어요. 확인이 필요해서 담당자 검토로 넘겼어요."


def _t_page_count(r: Any, status: str) -> str | None:
    m = r.measured or {}
    fp, op = m.get("file_pages"), m.get("order_pages")
    if status == "pass":
        return "페이지 수는 주문 내용과 맞아요."
    if status == "fail" and fp is not None and op is not None:
        return f"파일은 {fp}페이지인데 주문 기준은 {op}페이지예요. 어떤 구성으로 진행할지 알려주세요."
    if status == "fail":
        return "페이지 수가 주문 내용과 달라요. 어떤 구성으로 진행할지 알려주세요."
    if status == "warn":
        return "페이지 수에 주의가 필요해요."
    return "페이지 수를 세지 못했어요. 확인이 필요해서 담당자 검토로 넘겼어요."


def _t_transparency(r: Any, status: str) -> str | None:
    n = (r.measured or {}).get("count", 0)
    if status == "pass":
        return "투명 효과 없이 깔끔하게 준비돼 있어요."
    if status in ("warn", "fail"):
        return (
            f"투명 효과가 {n}곳에 쓰였어요. 인쇄 전에 병합(플래튼) 처리가 들어가는데, "
            "겹친 부분의 표현이 미세하게 달라질 수 있어요."
        )
    return "투명 효과 사용 여부를 판정하지 못했어요. 담당자 검토로 넘겼어요."


def _t_dieline(r: Any, status: str) -> str | None:
    m = r.measured or {}
    present = m.get("dieline_present")
    names = m.get("dieline_spot_names") or []
    if status == "pass":
        return f"칼선({', '.join(names)})이 잘 들어 있어요." if present and names else "칼선 확인 결과 문제 없어요."
    if status == "uncertain":
        if present:
            return (
                f"파일에 칼선({', '.join(names)})이 있는데 주문 내용과 맞는지 확인이 필요해요. "
                "담당자 검토로 넘겼어요."
            )
        return (
            "모양대로 자르는 재단(도무송)에 필요한 칼선이 파일에 없어요. "
            "사각 재단으로 진행할지, 칼선을 추가할지 확인이 필요해서 담당자 검토로 넘겼어요."
        )
    if status in ("warn", "fail"):
        return "칼선 구성에 문제가 있어요. 칼선 별색(CutContour)으로 다시 저장해 주세요."
    return None


def _t_min_line(r: Any, status: str) -> str | None:
    w = (r.measured or {}).get("min_width_pt")
    req = _fmt_num((r.required or {}).get("min_pt", 0.25))
    if status == "pass":
        return "선 굵기는 모두 인쇄 가능한 수준이에요."
    if status in ("warn", "fail"):
        return (
            f"아주 가는 선이 있어요(최소 {_fmt_num(w)}pt, 기준 {req}pt). "
            "인쇄하면 선이 끊기거나 안 보일 수 있어요."
        )
    return "선 굵기를 재지 못했어요. 담당자 검토로 넘겼어요."


_CHECK_TRANSLATORS = {
    "bleed": _t_bleed,
    "resolution": _t_resolution,
    "colorspace": _t_colorspace,
    "font_embed": _t_font_embed,
    "trim_safety": _t_trim_safety,
    "ink_total": _t_ink_total,
    "black_type": _t_black_type,
    "page_size": _t_page_size,
    "page_count": _t_page_count,
    "transparency": _t_transparency,
    "dieline": _t_dieline,
    "min_line": _t_min_line,
}


def translate_check(r: Any) -> str:
    """CheckResult 1건 → 고객 언어 문장. 어떤 (체크 × 상태) 조합도 빈손으로 돌려보내지 않는다."""
    status = str(r.status)
    fn = _CHECK_TRANSLATORS.get(r.check_id)
    if fn is not None:
        line = fn(r, status)
        if line:
            return line
    name = CHECK_NAMES.get(r.check_id, r.check_id)
    if status == "pass":
        return f"{name} 항목은 문제 없어요."
    if status == "warn":
        return f"{name} 항목에 주의가 필요해요. 진행은 가능하지만 결과물에 영향이 있을 수 있어요."
    if status == "fail":
        return f"{name} 항목이 인쇄 기준에 맞지 않아요. 파일 수정이 필요해요."
    return f"{name} 항목은 자동 판정이 어려워요. 확인이 필요해서 담당자 검토로 넘겼어요."


# ------------------------------------------------ 응답 조립 (규칙 폴백 템플릿)

#: 슬롯별 자연어 질문 문장 (없으면 generic)
_QUESTION_TEMPLATES: dict[str, str] = {
    "size": "사이즈는 어떻게 할까요?",
    "quantity": "수량은 얼마나 필요하세요?",
    "material": "용지는 어떤 걸로 할까요?",
    "coating": "코팅은 어떻게 할까요?",
    "cut_type": "재단은 어떤 모양으로 할까요?",
    "sides": "인쇄는 단면과 양면 중 어느 쪽으로 할까요?",
}

_GREETING = (
    "안녕하세요! AI 인쇄 접수 도우미예요. 스티커·명함·전단·포스터·라벨 인쇄를 도와드리고 있어요.\n"
    "어떤 상품이 필요하신지 말씀해 주시고, 인쇄할 PDF 파일이 있다면 지금 바로 올려주셔도 돼요."
)


def render_reply(
    directives: "ReplyDirectives",
    view: "SessionView",
    schema: ProductSchema | None,
    adapter: LLMAdapter | None = None,
) -> str:
    """지시서(directives) → 고객에게 보낼 한국어 응답 1건.

    adapter가 있으면 중형 모델(role="dialog")이 directives JSON을 번역하고,
    없으면 규칙 템플릿이 같은 재료로 문장을 조립한다. 숫자·판정은 어느 쪽이든
    directives의 확정값 그대로다 (철칙 2). LLM 응답이 비면 ParseError.
    """
    if adapter is not None:
        payload = {
            "directives": directives.model_dump(mode="json"),
            "session": view.model_dump(mode="json"),
            "product_display_name": schema.display_name if schema else None,
        }
        system = _load_prompt("dialog_v1.md")
        raw = adapter.complete(
            system,
            [{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
            role="dialog",
            max_tokens=800,
            temperature=0.6,
        )
        reply = raw.strip()
        if not reply:
            raise ParseError("대화 생성 결과가 비어 있음")
        return reply
    return _rule_render(directives, view, schema)


def _question_line(q: Any, schema: ProductSchema | None) -> str:
    sdef: SlotDef | None = schema.slots.get(q.slot) if schema else None
    display = q.display_name or _slot_display(q.slot, schema)
    base = _QUESTION_TEMPLATES.get(q.slot, f"{display}{_eun(display)} 어떻게 할까요?")
    unit = sdef.unit if sdef else ""
    if q.quick_options:
        opts = "/".join(f"{_fmt_num(o)}{unit}" for o in q.quick_options)
        return f"{base} {opts} 중에 골라주셔도 돼요."
    if sdef and sdef.choices:
        opts = ", ".join(_label(c) for c in sdef.choices)
        suffix = f"({unit})" if unit else ""
        return f"{base} {opts}{suffix} 중에서 고르실 수 있어요."
    return base


def _notice_line(code: str, schema: ProductSchema | None) -> str | None:
    """기계 코드(notices) → 부드러운 안내. 내부용 코드는 None(침묵)."""
    if code.startswith("invalid_value:"):
        name, _, value = code.split(":", 1)[1].partition("=")
        display = _slot_display(name, schema)
        line = f"말씀하신 {display} '{value}'{_eun(value)} 준비된 옵션에 없어요."
        sdef = schema.slots.get(name) if schema else None
        if sdef and sdef.choices:
            line += " " + ", ".join(_label(c) for c in sdef.choices) + " 중에서 골라주시면 돼요."
        return line
    if code.startswith("unknown_product:"):
        return "말씀하신 상품은 아직 준비 중이에요. 지금은 스티커, 명함, 전단, 포스터, 라벨을 도와드릴 수 있어요."
    if code.startswith("quote_missing:"):
        axis, _, value = code.split(":", 1)[1].partition("=")
        display = _slot_display(axis, schema)
        sdef = schema.slots.get(axis) if schema else None
        opts = None
        if sdef and sdef.quick_options:
            opts = "/".join(f"{_fmt_num(o)}{sdef.unit}" for o in sdef.quick_options)
        elif sdef and sdef.choices:
            opts = ", ".join(_label(c) for c in sdef.choices)
        line = f"말씀하신 {display} '{value}'{_eun(value)} 지금 가격표에 없어요."
        if opts:
            line += f" {opts} 중에서 골라주시면 바로 견적을 내드릴게요."
        return line
    if code.startswith("design_unsupported:"):
        return "시안 자동 생성은 지금 명함만 지원해요. 다른 상품은 완성된 파일을 올려주시면 도와드릴게요."
    if code == "slots_before_product":
        return "상품이 정해지면 말씀해 주신 사양을 바로 반영해드릴게요."
    if code == "file_received_need_product":
        return "파일은 잘 받았어요! 어떤 상품으로 인쇄할지 알려주시면 이어서 진행할게요."
    if code == "confirm_not_ready":
        return "아직 확정할 준비가 안 됐어요. 남은 항목부터 함께 채워볼게요."
    if code.startswith("autofix_unsupported"):
        return "그 항목은 자동 보정을 지원하지 않아요. 파일을 수정해 다시 올려주시거나 담당자 확인이 필요해요."
    if code == "autofix_no_file":
        return "보정할 파일이 아직 없어요. 먼저 PDF 파일을 올려주세요."
    return None  # quote_missing 등 내부 코드는 질문/에스컬레이션이 따로 안내한다


def _blocker_line(code: str) -> str:
    if code == "preflight_not_run":
        return "파일 검사가 아직 진행되지 않았어요. PDF 파일을 올려주세요."
    if code.startswith("preflight_fail:"):
        cid = code.split(":", 1)[1]
        return f"파일 검사에서 '{CHECK_NAMES.get(cid, cid)}' 항목이 아직 해결되지 않았어요."
    if code.startswith("preflight_uncertain:"):
        cid = code.split(":", 1)[1]
        return f"'{CHECK_NAMES.get(cid, cid)}' 항목은 담당자 확인이 필요해요."
    if code == "customer_not_confirmed":
        return "고객님의 확정이 아직 확인되지 않았어요."
    if code == "escalated":
        return "담당자 검토가 진행 중이에요. 검토가 끝나면 이어서 진행할 수 있어요."
    return code


def _quote_line(q: Any, schema: ProductSchema | None) -> str:
    line = f"견적은 부가세 포함 {_won(q.total)}이에요 (공급가 {_won(q.supply_amount)} + 부가세 {_won(q.vat)})."
    details: list[str] = []
    for item in q.lines:
        if item.item == "base":
            details.append(f"기본 인쇄비 {_won(item.amount)}")
        else:
            slot, _, value = item.item.partition(":")
            details.append(f"{_slot_display(slot, schema)} {_label(value)} 추가 {_won(item.amount)}")
    if len(details) > 1:
        line += " 내역은 " + ", ".join(details) + "이에요."  # 항상 '원'으로 끝남 (받침 ㄴ)
    return line


def _summary_line(view: "SessionView", schema: ProductSchema | None) -> str:
    slot_defs = schema.slots if schema else {}
    order = list(slot_defs) or list(view.slots or {})
    items: list[str] = []
    for name in order:
        entry = (view.slots or {}).get(name) or {}
        value = entry.get("value")
        if value is None:
            continue
        unit = slot_defs[name].unit if name in slot_defs else ""
        shown = _label(value)
        if unit and str(value).isdigit():
            shown += unit
        items.append(f"{_slot_display(name, schema)} {shown}")
    product_name = schema.display_name if schema else (view.product or "주문")
    return f"주문 내용을 정리해볼게요 — {product_name}: " + " / ".join(items) + "."


def _render_report(report: Any, offer_autofix: set[str]) -> list[str]:
    problems = [r for r in report.results if str(r.status) != "pass"]
    if not problems:
        return ["파일 검사 결과, 모든 항목을 통과했어요. 인쇄 진행에 문제가 없어요."]
    lines = ["파일을 검사해봤어요. 몇 가지 알려드릴 게 있어요."]
    for r in problems:
        line = translate_check(r)
        if r.check_id in offer_autofix and "드릴까요" not in line:
            line += " 자동 보정이 가능한 항목이에요 — 원하시면 바로 고쳐드릴게요."
        lines.append("- " + line)
    return lines


def _rule_render(d: "ReplyDirectives", view: "SessionView", schema: ProductSchema | None) -> str:
    """친절한 인쇄사 상담원 톤의 존댓말 템플릿 — 데모의 얼굴."""
    if d.kind == "greeting":
        return _GREETING

    if d.order_no:
        parts = [f"주문이 확정됐어요! 주문번호는 {d.order_no}예요."]
        if d.quote is not None and not d.quote.missing:
            parts.append(_quote_line(d.quote, schema))
        parts.append("접수해 주신 내용 그대로 인쇄 준비에 들어갈게요. 이용해 주셔서 감사합니다!")
        return "\n".join(parts)

    parts: list[str] = []

    if d.request_card_fields:
        parts.append(
            "명함 시안을 만들어드릴게요. 이름과 회사명을 알려주시면 시작할 수 있어요. "
            "직위·연락처·이메일도 함께 주시면 바로 반영할게요."
        )

    if d.design_generated:
        tmpl = f"'{d.design_template_name}' 스타일" if d.design_template_name else "기본 스타일"
        parts.append(
            f"주신 정보로 {tmpl} 명함 시안을 만들어봤어요. 아래 미리보기를 확인해 주세요. "
            "모던·클래식·미니멀 스타일 중에서 바꿔볼 수 있어요."
        )

    if d.offer_design and not d.design_generated:
        parts.append(
            "완성된 파일이 있으면 올려주셔도 되고, 없으시면 명함에 넣을 정보(이름·회사·직위·연락처)를 "
            "알려주시면 시안을 바로 만들어드려요."
        )

    if d.kind == "upload":
        parts.append("파일 잘 받았어요.")
    elif d.kind == "autofix":
        parts.append("요청하신 자동 보정을 적용하고 다시 검사했어요.")

    for code in d.notices:
        line = _notice_line(code, schema)
        if line:
            parts.append(line)

    # 리포트 상세 번역은 업로드/보정 직후에만 (매 턴 반복 방지 — cards 정책과 동일)
    if d.report is not None and d.kind in ("upload", "autofix"):
        parts.extend(_render_report(d.report, set(d.offer_autofix)))

    for c in d.conflicts:
        display = c.display_name or _slot_display(c.slot, schema)
        inferred, user = _label(c.inferred_value), _label(c.user_value)
        parts.append(
            f"{display} 확인이 필요해요 — 파일은 {inferred}인데 {user}{_ro(user)} 말씀하셨어요. "
            "어느 쪽으로 할까요?"
        )

    if d.auto_filled:
        filled = ", ".join(
            f"{_slot_display(af.slot, schema)}{_eun(_slot_display(af.slot, schema))} "
            f"{_label(af.value)}{_ro(_label(af.value))}"
            for af in d.auto_filled
        )
        parts.append(f"{filled} 기본 적용해뒀어요. 다른 걸 원하시면 언제든 말씀해 주세요.")

    if d.questions:
        if len(d.questions) > 1:
            parts.append("몇 가지만 여쭤볼게요.")
        for q in d.questions:
            parts.append(_question_line(q, schema))

    if d.quote is not None and not d.quote.missing:
        parts.append(_quote_line(d.quote, schema))

    if d.awaiting_confirm:
        parts.append(_summary_line(view, schema) + " 이대로 진행할까요?")

    if d.gate_blockers:
        parts.append("주문 확정 전에 해결할 것이 남아 있어요.")
        parts.extend("- " + _blocker_line(b) for b in d.gate_blockers)

    if d.escalation_reasons:
        parts.append(
            "확인이 필요한 부분이 있어서 담당자 검토 큐로 넘겼어요. "
            "담당자가 살펴본 뒤 이어서 안내드릴게요."
        )

    if d.request_product:
        parts.append("어떤 상품을 인쇄할까요? 스티커, 명함, 전단, 포스터, 라벨 중에 말씀해 주세요.")
    if d.request_file and not d.awaiting_confirm:
        parts.append("인쇄할 PDF 파일을 올려주시면 바로 검사해서 알려드릴게요.")

    if not parts:
        parts.append("네, 확인했어요. 이어서 진행할게요.")
    return "\n".join(parts)
