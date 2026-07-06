"""bleed — 재단여백(도련) ≥3mm 사방 검사.

측정: TrimBox와 바깥 박스(MediaBox·BleedBox 중 더 넓은 쪽) 사이의
4방향 간격을 mm로 실측한다. 다중 페이지면 전 페이지를 검사하고,
방향별로 전 페이지 중 최소값을 measured에 담는다.

판정: 4방향 모두 3.0mm 이상(허용오차 -0.1mm → 실질 2.9mm)이면 pass.
한 방향이라도 미달이면 fail + autofix(extend_bleed, 래스터 연장) 제안.

TrimBox가 아예 없는 파일은 실무에서 매우 흔하다 — 이 경우 trim==media로
간주하여 재단여백 0mm → fail 처리하고 detail에 명시한다.
"""

from __future__ import annotations

import math

from core.preflight.engine import CheckContext, pt_to_mm, register_check, result
from core.preflight.report import AutofixInfo, CheckResult, CheckStatus

#: 요구 재단여백 (사방, mm)
REQUIRED_MM = 3.0
#: 허용오차 — 이 값만큼 부족해도 통과 (좌표 반올림·생성 툴 오차 흡수)
TOLERANCE_MM = 0.1
#: 파일이 재단선(주문 규격)보다 커서 '재단여백을 이미 포함'한 경우의 최소 인정치.
#: 이만큼이라도 여분이 있으면 '여백이 들어간 파일'로 보고 통과 — 고객이 일부러 크게 넣은 것.
INTENTIONAL_BLEED_MIN_MM = 1.0
#: 부동소수점 경계 흔들림 방지용
_EPS = 1e-6

_SIDES = ("left", "right", "top", "bottom")


def _insets_from_boxes(trim, media, bleed_box) -> dict[str, float]:
    """TrimBox 기준 4방향 여백(mm) — 바깥 박스는 MediaBox·BleedBox 중 넓은 쪽."""
    outer = media
    if bleed_box is not None:
        outer = (
            min(outer[0], bleed_box[0]),
            min(outer[1], bleed_box[1]),
            max(outer[2], bleed_box[2]),
            max(outer[3], bleed_box[3]),
        )
    return {
        "left": pt_to_mm(trim[0] - outer[0]),
        "bottom": pt_to_mm(trim[1] - outer[1]),
        "right": pt_to_mm(outer[2] - trim[2]),
        "top": pt_to_mm(outer[3] - trim[3]),
    }


def _insets_from_order(media, order_mm) -> dict[str, float] | None:
    """TrimBox가 없을 때, 주문 규격(재단선)을 미디어 중앙에 놓고 재단여백을 잰다.

    파일이 규격보다 큰 방향의 여분이 곧 재단여백이다(예: 90x50 재단선 + 53x94 파일 → 사방 1.5~2mm).
    규격이 미디어에 아예 안 들어가면(파일이 더 작음) None → 여백 없음으로 폴백.
    """
    mw = pt_to_mm(media[2] - media[0])
    mh = pt_to_mm(media[3] - media[1])
    ow, oh = float(order_mm[0]), float(order_mm[1])
    cands = [
        (tw, th, (mw - tw) + (mh - th))
        for tw, th in ((ow, oh), (oh, ow))
        if tw <= mw + 0.5 and th <= mh + 0.5
    ]
    if not cands:
        return None
    tw, th, _slack = min(cands, key=lambda c: c[2])  # 가장 꽉 차는 방향 = 실제 배치
    lr = max(0.0, (mw - tw) / 2.0)
    tb = max(0.0, (mh - th) / 2.0)
    return {"left": lr, "right": lr, "top": tb, "bottom": tb}


def _inherited_mediabox(ctx: CheckContext, page_index: int):
    """페이지 딕셔너리에 /MediaBox가 없을 때 pikepdf의 상속 해석으로 폴백."""
    try:
        mb = ctx.pdf.pages[page_index].mediabox  # pikepdf가 /Pages 트리 상속을 처리
        x0, y0, x1, y1 = (float(v) for v in mb)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    except Exception:
        return None


def _measure(ctx: CheckContext) -> CheckResult:
    # 방향별 최소 여백 (전 페이지 기준)
    min_insets = {s: math.inf for s in _SIDES}
    bad_pages: list[int] = []          # 여백 미달 페이지
    no_trim_pages: list[int] = []      # TrimBox 부재 → trim==media 간주 페이지
    order_bleed_pages: list[int] = []  # TrimBox 없지만 규격보다 커서 여백 인정한 페이지
    no_media_pages: list[int] = []     # MediaBox조차 해석 불가 → 판단 불가 페이지

    order_mm = ctx.order.size_mm if ctx.order else None

    page_count = ctx.page_count
    if page_count == 0:
        return result(
            "bleed",
            CheckStatus.UNCERTAIN,
            required={"min_mm": REQUIRED_MM},
            detail="페이지가 없는 PDF — 재단여백 판단 불가",
        )

    for i in range(page_count):
        boxes = ctx.page_boxes(i)
        media = boxes.get("media") or _inherited_mediabox(ctx, i)
        if media is None:
            no_media_pages.append(i)
            continue

        trim = boxes.get("trim")
        bleed_box = boxes.get("bleed")
        order_derived = False

        # 1) 파일 박스(TrimBox/BleedBox) 기준 여백 — 디자이너가 도련을 제대로 잡은 경우
        box_insets = _insets_from_boxes(trim if trim is not None else media, media, bleed_box)
        box_min = min(box_insets.values())

        # 2) 박스상 여백이 사실상 0인데(도련 미표기·trim==media) 파일이 재단선(주문 규격)보다
        #    크면, 그 여분이 곧 재단여백이다 — 고객이 크기에 여백을 포함해 넣은 것.
        if box_min + _EPS < INTENTIONAL_BLEED_MIN_MM and order_mm:
            derived = _insets_from_order(media, order_mm)
            if derived is not None and min(derived.values()) + _EPS >= INTENTIONAL_BLEED_MIN_MM:
                box_insets = derived
                order_derived = True
                order_bleed_pages.append(i)

        if not order_derived and trim is None:
            no_trim_pages.append(i)

        insets_mm = box_insets
        for side, v in insets_mm.items():
            min_insets[side] = min(min_insets[side], v)
        # 규격보다 커서 여백을 이미 포함한 파일은 완화 기준, 그 외는 3mm 기준
        floor = INTENTIONAL_BLEED_MIN_MM if order_derived else REQUIRED_MM - TOLERANCE_MM
        if min(insets_mm.values()) + _EPS < floor:
            bad_pages.append(i)

    if len(no_media_pages) == page_count:
        # 전 페이지에서 MediaBox 해석 불가 — 측정 자체가 성립하지 않음
        return result(
            "bleed",
            CheckStatus.UNCERTAIN,
            required={"min_mm": REQUIRED_MM},
            pages=no_media_pages,
            detail="전 페이지에서 MediaBox를 해석할 수 없음 — 재단여백 판단 불가",
        )
    if no_media_pages:
        # 일부 페이지만 측정 불가 → 보수적으로 판단 불가 처리 (에스컬레이션)
        return result(
            "bleed",
            CheckStatus.UNCERTAIN,
            required={"min_mm": REQUIRED_MM},
            pages=no_media_pages,
            detail=f"일부 페이지의 MediaBox 해석 불가 (pages={no_media_pages}) — 재단여백 판단 불가",
        )

    measured = {
        "insets_mm": {s: round(min_insets[s], 3) for s in _SIDES},
        "min_mm": round(min(min_insets.values()), 3),
    }
    if order_bleed_pages:
        # 규격보다 큰 파일 = 재단여백을 이미 포함 (검판원·UI가 긍정적으로 표시하게)
        measured["includes_bleed"] = True
    required = {"min_mm": REQUIRED_MM}

    notes: list[str] = []
    if order_bleed_pages:
        notes.append(
            "재단선(주문 규격)보다 큰 파일 — 그 여분을 재단여백으로 인정: "
            f"사방 최소 {measured['min_mm']}mm 확보됨"
        )
    if no_trim_pages:
        notes.append(
            f"TrimBox 없음 → trim==MediaBox로 간주(재단여백 0mm): pages={no_trim_pages}"
        )
    notes.append(
        "방향별 최소 여백(mm, 전 페이지 기준): "
        + ", ".join(f"{s}={measured['insets_mm'][s]}" for s in _SIDES)
    )
    notes.append(f"기준 {REQUIRED_MM}mm (허용오차 -{TOLERANCE_MM}mm)")

    if not bad_pages:
        return result(
            "bleed",
            CheckStatus.PASS,
            measured=measured,
            required=required,
            detail="; ".join(notes),
        )

    return result(
        "bleed",
        CheckStatus.FAIL,
        measured=measured,
        required=required,
        pages=bad_pages,
        autofix=AutofixInfo(
            available=True,
            fix_id="extend_bleed",
            note=(
                "페이지를 래스터화한 뒤 가장자리 픽셀을 바깥으로 복제(edge replicate)해 "
                "사방 3mm 재단여백을 만든다. 재단선 안쪽 디자인은 그대로 유지되어 "
                "육안 차이가 없다."
            ),
        ),
        detail="; ".join(notes),
    )


@register_check("bleed")
def check_bleed(ctx: CheckContext) -> CheckResult:
    """재단여백 ≥3mm 사방. 내부 예외는 uncertain으로 격리한다."""
    try:
        return _measure(ctx)
    except Exception as e:  # 어떤 상황에서도 예외를 밖으로 내보내지 않는다
        return result(
            "bleed",
            CheckStatus.UNCERTAIN,
            required={"min_mm": REQUIRED_MM},
            detail=f"측정 실패: {type(e).__name__}: {e}",
        )
