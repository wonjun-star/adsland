"""애즈랜드(adsland.com) 작업 가이드 기반 검수 규칙 + 수정 안내.

이 서비스의 핵심: 고객이 어떤 파일을 넣든, 애즈랜드 작업 가이드에 **맞는 건 통과**시키고
**안 맞는 건 "이렇게 바꾸세요"를 가이드 근거와 함께** 안내한다. 측정·판정은 결정론적
체크가 하고(core/preflight/checks/*), 이 모듈은 두 가지만 제공한다:

  1) PRODUCT_RULES — 품목별 임계값(도련·안전여백·총잉크량·선굵기 등). 체크가 이 값을 쓴다.
  2) REMEDIATION  — 위반 시 가이드를 인용한 수정 안내(왜·어떻게·프로그램별 방법). LLM은 번역만.

수치 출처 원칙(철칙): **가이드 텍스트에서 확인된 값만** guide_confirmed=True로 표기한다.
가이드가 이미지(그림)로만 안내해 텍스트로 못 가져온 값은 업계 표준으로 두고
guide_confirmed=False로 명시한다 — 지어낸 값을 확인된 값처럼 쓰지 않는다.

가이드 출처(2026-07 확인):
  일러스트  guide_01_01_ill_win · 포토샵 guide_01_01_pho_win · 인디자인 guide_01_01_ind
  PDF guide_01_03_prf · 품목별 guide_01_02_* (명함 card / 스티커 stc·stc2·stc3 / 전단 let ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field

_BASE = "https://www.adsland.com/bbs/content.php?co_id="

#: 가이드 페이지 URL (안내 문구에서 근거로 링크)
GUIDE_URLS: dict[str, str] = {
    "illustrator": _BASE + "guide_01_01_ill_win",
    "photoshop": _BASE + "guide_01_01_pho_win",
    "indesign": _BASE + "guide_01_01_ind",
    "pdf": _BASE + "guide_01_03_prf",
    "namecard": _BASE + "guide_01_02_card",
    "sticker": _BASE + "guide_01_02_stc",
    "sticker_diecut": _BASE + "guide_01_02_stc2",
    "flyer": _BASE + "guide_01_02_let",
    "leaflet": _BASE + "guide_01_02_leaf",
    "catalog": _BASE + "guide_01_02_cata",
    "memopad": _BASE + "guide_01_02_pads",
    "dieline_download": _BASE + "guide_02_14",
    "finishing": _BASE + "guide_03_01",
    "paper": _BASE + "guide_04_01",
}


#: 애즈랜드 주문 페이지에서 확보한 **재단(완성) 규격 크기 mm** (편집=재단+도련, 안전=재단-여백).
#: 주문 페이지 spec 테이블(HTML)·톰슨 가이드에서 직접 파싱/판독한 값. (지어낸 값 아님)
#: 참고용 — 카탈로그/가격표를 애즈랜드 전 규격에 맞추는 것은 별도 작업(가격 매핑 필요).
STANDARD_SIZES: dict[str, dict] = {
    "namecard": {
        "mode": "free_input",           # 고정 규격표 없이 자유 입력
        "min_mm": (85.0, 45.0), "max_mm": (150.0, 100.0),
        "presets_mm": [(90.0, 50.0), (86.0, 52.0), (88.0, 54.0)],  # 표준 프리셋 (86×52 주의)
        "bleed_mm": 1.0, "source": "order_IC00001 / 용지 스노우지 작업92×52=재단90×50",
    },
    "postcard": {  # 엽서
        "mode": "free_input",
        "min_mm": (85.0, 45.0), "max_mm": (150.0, 100.0),
        "source": "order_IC00113",
    },
    "memopad": {  # 떡메모지 (재단 기준)
        "sizes_mm": [(80, 80), (90, 90), (100, 100), (100, 90), (106, 80),
                     (100, 140), (146.5, 210), (210, 297), (50, 140), (70, 180), (85, 190)],
        "bleed_mm": 2.0, "safety_mm": 2.0, "source": "order_IC00025",
    },
    "flyer": {  # 전단·포스터 공통 A/B 시리즈 (재단)
        "sizes_mm": [(600, 900), (420, 600), (297, 420), (210, 297), (146, 210), (103, 146),
                     (520, 760), (370, 520), (257, 368), (182, 257), (128, 182), (90, 128)],
        "bleed_mm": 2.0, "safety_mm": 2.0, "source": "order_IC00017/IC00018 / guide_02_14",
    },
    "sticker_square": {  # 사각재단 스티커 (재단)
        "sizes_mm": [(50, 50), (80, 50), (90, 55), (90, 70), (90, 90), (90, 100), (90, 150)],
        "bleed_mm": 3.0, "source": "order_IC00030",
    },
    "sticker_diecut": {  # 도무송 스티커: 칼선=재단-3mm, 편집=재단+3mm
        "sizes_mm": [(53, 32), (50, 50), (80, 50), (90, 55)],
        "bleed_mm": 3.0, "dieline_inset_mm": 3.0, "min_diecut_mm": 10.0,
        "source": "order_IC00031 / thomson_guide",
    },
}

#: 도무송(칼선) 규칙 — 톰슨 가이드 OCR 확정
DIELINE_RULES = {
    "color": "K100",                 # 칼선은 검정 100% (명명 별색 아님)
    "min_size_mm": 10.0,             # 도무송 최소 10mm
    "gap_mm": 3.0,                   # 도형 간 최소 간격 3mm
    "sticker_inset_mm": 3.0,         # 스티커: 칼선=재단-사방3mm
    "files": ["도무송 위치용", "인쇄용 앞(칼선 색없음)", "인쇄용 뒤", "도무송 도안(K100)"],
    "source": "dieline_download",    # guide_03_03 톰슨 가이드
}


@dataclass(frozen=True)
class ProductRule:
    """품목별 검수 임계값. None이면 체크가 기본값을 쓴다.

    guide_confirmed=False인 값은 가이드 이미지에만 있어 텍스트로 확인 못 한 것(업계 표준).
    """

    #: 재단여백(도련) 최소 mm — 인디자인 가이드: 명함 1 / 전단 2 / 스티커(도무송·배경사각) 3
    bleed_mm: float | None = None
    #: 안전여백(재단선 안쪽, 중요 정보 배치 한계) mm — 품목별 가이드
    safety_mm: float | None = None
    #: 총잉크량(TAC) 상한 % — 전단·포스터·리플렛 250, 그 외 기본 300
    max_ink_percent: float | None = None
    #: 권장 최소 선굵기 pt — 공통 "0.5pt 미만 선 금지"(포토샵·인디자인)
    min_line_pt: float | None = None
    #: 도무송 정재단 시 칼선보다 바깥 여백 mm (정재단=칼선+사방 3mm)
    diecut_outer_mm: float | None = None
    #: 값의 근거가 가이드 텍스트로 확인됐는가
    guide_confirmed: bool = True
    #: 근거 가이드 키(GUIDE_URLS)
    source: str = ""
    #: 사람이 읽는 근거 메모
    note: str = ""


# 품목별 규칙 — 우리 카탈로그 product id 기준.
# 확인된 값(guide_confirmed=True)과 표준 추정값(False)을 구분해 둔다.
PRODUCT_RULES: dict[str, ProductRule] = {
    # 명함: 인디자인 '명함류 1mm', 품목별 안전여백 2mm, 0.5pt 미만 먹1도
    "namecard": ProductRule(
        bleed_mm=1.0, safety_mm=2.0, max_ink_percent=300.0, min_line_pt=0.5,
        source="namecard", note="인디자인 도련 1mm / 명함 안전여백 2mm (TAC 300%는 가이드 미명시 표준)",
    ),
    # 전단: 규격표 확인(A4 재단210×297 / 안전206×293 / 편집214×301) → 도련 2mm·안전 2mm, TAC 250%
    "flyer": ProductRule(
        bleed_mm=2.0, safety_mm=2.0, max_ink_percent=250.0, min_line_pt=0.5,
        source="flyer", note="전단 규격표 확인: 도련 2mm·안전여백 2mm / CMYK 총합 250% 이하",
    ),
    # 포스터: 전단과 같은 규격표(A/B 시리즈)
    "poster": ProductRule(
        bleed_mm=2.0, safety_mm=2.0, max_ink_percent=250.0, min_line_pt=0.5,
        source="flyer", note="포스터 규격표 확인: 도련 2mm·안전 2mm / 총잉크 250%",
    ),
    # 스티커(사각): 도련 3mm, 안전여백 2mm, 테두리 재단선 안쪽 3mm
    "sticker": ProductRule(
        bleed_mm=3.0, safety_mm=2.0, max_ink_percent=300.0, min_line_pt=0.5,
        diecut_outer_mm=3.0, source="sticker",
        note="스티커 도련 3mm / 사각 안전여백 2mm(도무송 3mm) / 정재단 칼선+3mm",
    ),
    # 라벨: 가이드에 독립 항목 없음 — 스티커 규칙 준용(추론). TAC 300은 가이드 미명시 표준.
    "label": ProductRule(
        bleed_mm=3.0, safety_mm=2.0, max_ink_percent=300.0, min_line_pt=0.5,
        diecut_outer_mm=3.0, guide_confirmed=False, source="sticker",
        note="라벨=스티커 규칙 준용(가이드 독립 항목 없음) / TAC 300%는 가이드 미명시 표준",
    ),
    # 엽서: 낱장 인쇄물(전단류에 준함) — 도련 수치 가이드 이미지 기반, 표준 2mm
    "postcard": ProductRule(
        bleed_mm=2.0, safety_mm=3.0, max_ink_percent=300.0, min_line_pt=0.5,
        guide_confirmed=False, source="flyer", note="엽서 규격 크기·도련은 가이드 이미지 기반(표준 추정)",
    ),
    # 떡메모지: 주문 규격표 확인(재단80×80 / 안전76×76 / 편집84×84) → 도련 2mm·안전 2mm
    "memopad": ProductRule(
        bleed_mm=2.0, safety_mm=2.0, max_ink_percent=300.0, min_line_pt=0.5,
        source="memopad", note="떡메모지 규격표 확인: 재단80×80/안전76×76/편집84×84 → 도련 2mm·안전 2mm",
    ),
    # 포토카드: 가이드에 별도 항목 없음 — 표준 추정
    "photocard": ProductRule(
        bleed_mm=2.0, safety_mm=3.0, max_ink_percent=300.0, min_line_pt=0.5,
        guide_confirmed=False, source="", note="가이드에 별도 항목 없음(표준 추정)",
    ),
    # 현수막(대형 실사): 가이드에 별도 항목 없음 — 대형은 도련 넉넉히(표준 추정)
    "banner": ProductRule(
        bleed_mm=3.0, safety_mm=5.0, max_ink_percent=300.0, min_line_pt=0.5,
        guide_confirmed=False, source="", note="대형 실사, 가이드 별도 항목 없음(표준 추정)",
    ),
}

#: 품목·기본값 모두 없을 때 쓰는 최종 기본값 (보수적 표준)
DEFAULT_RULE = ProductRule(
    bleed_mm=2.0, safety_mm=3.0, max_ink_percent=300.0, min_line_pt=0.5,
    guide_confirmed=False, source="", note="기본값(표준)",
)


def rule_for(product: str | None) -> ProductRule:
    """품목 id → 검수 임계값. 미등록 품목은 DEFAULT_RULE."""
    if product and product in PRODUCT_RULES:
        return PRODUCT_RULES[product]
    return DEFAULT_RULE


def safety_mm_for(product: str | None, cut_type: str | None = None) -> float:
    """안전여백 — 스티커/라벨 도무송(die_cut)은 3mm, 사각은 2mm."""
    r = rule_for(product)
    base = r.safety_mm if r.safety_mm is not None else DEFAULT_RULE.safety_mm
    if product in ("sticker", "label") and cut_type == "die_cut":
        return 3.0
    return float(base)


# ---------------------------------------------------------------- 수정 안내(가이드 인용)


@dataclass(frozen=True)
class Remediation:
    """검수 항목이 걸렸을 때 고객에게 줄 '가이드 근거 + 어떻게 고치는지'.

    LLM은 이 내용을 자연스러운 말로 옮기기만 한다(숫자·근거는 그대로). autofixable=True면
    우리가 파일을 직접 보정할 수 있고, False면 고객이 원본에서 고쳐 다시 올려야 한다.
    """

    title: str                       # 항목 이름 (고객 언어)
    rule: str                        # 가이드가 요구하는 것 (한 줄)
    why: str                         # 왜 필요한지 (실물에 미치는 영향)
    how_to_fix: dict[str, str] = field(default_factory=dict)  # 프로그램별 방법
    autofixable: bool = False        # 우리가 자동 보정 가능한가
    source: str = ""                 # 근거 가이드 키(GUIDE_URLS)


#: 체크 id → 가이드 근거 수정 안내. (프로그램별 방법은 일러스트/포토샵/인디자인 가이드 인용)
REMEDIATION: dict[str, Remediation] = {
    "colorspace": Remediation(
        title="색상 모드(CMYK)",
        rule="문서와 모든 오브젝트를 CMYK로 설정 (RGB·미변환 별색은 인쇄 색이 달라짐)",
        why="RGB로 만든 색은 인쇄(CMYK)에서 그대로 안 나와서, 화면과 실물 색이 달라져요.",
        how_to_fix={
            "illustrator": "파일 > 문서 색상 모드 > CMYK 색상. 오브젝트 색도 CMYK 견본으로 지정.",
            "photoshop": "이미지 > 모드 > CMYK 색상. 컬러 프로파일은 'CMYK Japan Color 2001 Coated'.",
            "indesign": "색상은 C·M·Y·K 수치로 지정. 별색(PANTONE)은 색상 유형을 '원색(CMYK)'으로 변환.",
        },
        autofixable=True,  # RGB→CMYK 근사 변환(GCR) 자동 적용 가능 (색 약간 달라질 수 있음 고지)
        source="photoshop",
    ),
    "resolution": Remediation(
        title="이미지 해상도(300dpi)",
        rule="인쇄용 이미지는 원본 크기 기준 300dpi 이상 (72dpi 웹 이미지는 확대해도 안 좋아짐)",
        why="해상도가 낮으면 인쇄물에서 사진·이미지가 흐릿하거나 계단처럼 깨져 보여요.",
        how_to_fix={
            "photoshop": "이미지 > 이미지 크기에서 원본 크기 기준 300dpi로. 72dpi를 올리는 건 효과 없어요 — 고해상 원본으로 다시.",
            "common": "웹에서 받은 낮은 해상도 이미지는 인쇄용 고해상 원본으로 교체해 주세요.",
        },
        autofixable=False,
        source="pdf",
    ),
    "bleed": Remediation(
        title="재단여백(도련)",
        rule="재단선 밖으로 여백을 둬야 함 (명함 1mm / 전단 2mm / 스티커 3mm). 배경·이미지는 재단선 밖까지 채우기",
        why="재단은 1~2mm 밀릴 수 있어요. 여백이 없으면 잘린 가장자리에 흰 줄이 생겨요.",
        how_to_fix={
            "indesign": "새 문서에서 도련을 품목값(명함1/전단2/스티커3mm)으로. 배경은 도련선까지 채우기.",
            "common": "배경·이미지를 재단선 밖으로 조금 더 늘려 주세요. (여백만 부족하면 자동으로 채워드릴 수 있어요)",
        },
        autofixable=True,  # extend_bleed (가장자리 픽셀 복제로 여백 생성)
        source="indesign",
    ),
    "trim_safety": Remediation(
        title="안전여백(중요 정보 배치)",
        rule="글자·로고 등 중요 정보는 재단선 안쪽으로 (명함 2mm / 도무송·리플렛 3mm) 들여 배치",
        why="재단이 밀리면 가장자리에 붙은 글자·로고가 잘릴 수 있어요.",
        how_to_fix={
            "common": "잘리면 안 되는 글자·로고를 재단선에서 조금 안쪽으로 옮겨 주세요.",
        },
        autofixable=False,
        source="namecard",
    ),
    "ink_total": Remediation(
        title="총잉크량(TAC)",
        rule="CMYK 잉크 합계는 상한 이하 (전단·포스터·리플렛 250%, 그 외 300%)",
        why="잉크가 너무 많으면 마르지 않아 번지거나 뒷묻음이 생겨요.",
        how_to_fix={
            "common": "어두운 영역의 C·M·Y·K 합이 상한을 넘지 않게 낮춰 주세요. (진한 검정은 K100+C10 권장)",
        },
        autofixable=False,
        source="flyer",
    ),
    "min_line": Remediation(
        title="선 굵기",
        rule="0.5pt 미만 얇은 선은 피하기 (점선처럼 끊겨 보임). 얇은 검정 선은 먹1도로",
        why="너무 얇은 선은 인쇄에서 제대로 안 찍히거나 끊겨 보여요.",
        how_to_fix={
            "common": "얇은 선은 0.5pt 이상으로 굵혀 주세요. 얇은 검정선은 K100(먹1도)으로.",
        },
        autofixable=False,
        source="namecard",
    ),
    "black_type": Remediation(
        title="검정 텍스트(먹1도)",
        rule="일반 텍스트는 K100(먹1도). C·M·Y 섞인 4도 블랙은 핀이 어긋나 번져 보임",
        why="검정 글씨에 다른 색이 섞이면 인쇄 핀이 어긋나 글자가 겹쳐 흐리게 나와요.",
        how_to_fix={
            "common": (
                "검정 텍스트는 K100(먹1도)으로. 넓은 검정 배경은 K100+C10(진한 검정) 권장. "
                "K100은 자동 오버프린트되니, 배경 위 검정이 사라지지 않게 하려면 K99 또는 K100+C1로."
            ),
        },
        autofixable=False,
        source="indesign",
    ),
    "font_embed": Remediation(
        title="글꼴(아웃라인)",
        rule="모든 문자는 아웃라인(윤곽선) 처리 — 폰트가 없는 환경에서도 글자가 바뀌지 않게",
        why="폰트가 임베드/아웃라인 안 되면 저희 쪽에서 글자가 다른 서체로 바뀌거나 깨질 수 있어요.",
        how_to_fix={
            "illustrator": "문자 선택 후 문자 > 윤곽선 만들기 (Shift+Ctrl+O). 처리 후엔 오타 수정 불가하니 원본 백업.",
            "indesign": "텍스트 선택 후 윤곽선 만들기 (Ctrl+Shift+O).",
            "photoshop": "텍스트 레이어 우클릭 > 문자 래스터화(Rasterize Type).",
        },
        autofixable=False,
        source="indesign",
    ),
    "transparency": Remediation(
        title="투명도·오버프린트",
        rule="투명도는 병합(플래튼), 오버프린트는 해제. K100은 자동 오버프린트되니 주의",
        why="투명 효과나 오버프린트가 남으면 인쇄에서 색이 더 어둡거나 흰 글씨가 사라질 수 있어요.",
        how_to_fix={
            "illustrator": "오버프린트 해제(윈도우 > 특성). 투명 효과는 병합.",
            "indesign": "보기 > 오버프린트 미리보기로 확인. 투명도 혼합 공간=문서 CMYK, 고해상 병합.",
            "photoshop": "저장 전 모든 레이어 병합.",
        },
        autofixable=False,
        source="indesign",
    ),
    "dieline": Remediation(
        title="칼선(도무송)",
        rule="도무송은 칼선이 필요. 칼선 모서리는 3R 이상 둥글게, 칼선 간 3mm 이상, 정재단은 칼선보다 사방 3mm 크게",
        why="칼선이 없거나 규칙에 안 맞으면 모양대로 재단(도무송)을 진행할 수 없어요.",
        how_to_fix={
            "common": "재단 형태를 '사각'으로 하시거나, 도무송이면 칼선을 넣어 다시 올려 주세요. (칼선 템플릿은 애즈랜드에서 받을 수 있어요)",
        },
        autofixable=False,
        source="sticker_diecut",
    ),
    "page_size": Remediation(
        title="재단 크기(규격)",
        rule="파일 재단 크기 = 주문 규격 (재단여백 포함해 조금 큰 건 정상)",
        why="파일 크기가 주문 규격과 많이 다르면 원하는 크기로 안 나와요.",
        how_to_fix={
            "common": "주문하신 규격에 맞춰 파일 크기를 맞추거나, 파일 크기에 맞는 규격을 골라 주세요.",
        },
        autofixable=False,
        source="namecard",
    ),
    "page_count": Remediation(
        title="페이지 수",
        rule="파일 페이지 수 = 주문한 면 수 (단면 1장 / 양면 2장). 앞·뒷면·칼선은 각각 별도 파일",
        why="페이지 수가 주문과 다르면 어느 면을 인쇄할지 확정할 수 없어요.",
        how_to_fix={
            "common": "단면은 1장, 양면은 앞·뒤 2장으로 올려 주세요.",
        },
        autofixable=False,
        source="photoshop",
    ),
}


def remediation_for(check_id: str) -> Remediation | None:
    return REMEDIATION.get(check_id)


def guide_url(key: str) -> str:
    return GUIDE_URLS.get(key, "")
