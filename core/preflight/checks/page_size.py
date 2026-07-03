"""page_size 체크 — 파일 재단 크기 = 주문 규격 (±0.5mm).

측정: 페이지별 TrimBox 크기(mm). TrimBox가 없으면 MediaBox 크기로 폴백하고
detail에 명시한다 (엔진의 trim_size_mm은 TrimBox 전용이라 여기서 직접 폴백 처리).

판정:
- 주문 규격(ctx.order.size_mm)이 없으면 비교 생략 → pass.
- 가로세로가 뒤바뀐 일치(회전 배치)도 일치로 인정하되 measured["rotated"]=True.
- 페이지 간 재단 크기가 서로 다르면(회전 동일시, ±0.5mm) 그 자체로 fail.
- 주문 규격과 ±0.5mm 초과로 다르면 fail.
- 크기 문제는 임의 보정 불가(autofix 없음) — 고객 확인 질문 대상.
"""

from __future__ import annotations

from typing import Any

from core.preflight.engine import CheckContext, pt_to_mm, register_check, result
from core.preflight.report import CheckResult, CheckStatus

#: 허용 오차 (mm) — PLAN §6: 주문 규격과 일치(±0.5mm)
TOLERANCE_MM = 0.5


def _near(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE_MM


def _same_size(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """회전(가로세로 교환)을 동일시한 크기 비교 — 페이지 간 일관성 검사용."""
    a_lo, a_hi = sorted(a)
    b_lo, b_hi = sorted(b)
    return _near(a_lo, b_lo) and _near(a_hi, b_hi)


@register_check("page_size")
def check_page_size(ctx: CheckContext) -> CheckResult:
    # 어떤 입력(깨진 PDF 포함)에서도 예외가 밖으로 나가지 않게 — 판단 불가는 uncertain
    try:
        return _check(ctx)
    except Exception as e:
        return result(
            "page_size",
            CheckStatus.UNCERTAIN,
            required={"tolerance_mm": TOLERANCE_MM},
            detail=f"크기 측정 실패: {type(e).__name__}: {e}",
        )


def _check(ctx: CheckContext) -> CheckResult:
    required = {"tolerance_mm": TOLERANCE_MM}

    if ctx.page_count == 0:
        return result(
            "page_size",
            CheckStatus.UNCERTAIN,
            required=required,
            detail="페이지가 없어 크기 측정 불가",
        )

    # 페이지별 재단 크기 수집: (페이지 번호, (w, h) mm, 사용한 박스)
    per_page: list[tuple[int, tuple[float, float], str]] = []
    no_box_pages: list[int] = []
    for i in range(ctx.page_count):
        boxes = ctx.page_boxes(i)
        box, used = boxes.get("trim"), "trim"
        if box is None:
            box, used = boxes.get("media"), "media"
        if box is None:
            # MediaBox조차 페이지 딕셔너리에 없음 (상속 등) — 해당 페이지는 측정 제외
            no_box_pages.append(i)
            continue
        per_page.append((i, (pt_to_mm(box[2] - box[0]), pt_to_mm(box[3] - box[1])), used))

    if not per_page:
        return result(
            "page_size",
            CheckStatus.UNCERTAIN,
            required=required,
            pages=no_box_pages,
            detail="전 페이지에 TrimBox/MediaBox 없음 — 크기 측정 불가",
        )

    notes: list[str] = []
    media_pages = [i for i, _, used in per_page if used == "media"]
    if media_pages:
        notes.append(f"TrimBox 없음 → MediaBox 크기 사용 (페이지 {media_pages})")
    if no_box_pages:
        notes.append(f"박스 정보가 없는 페이지 {no_box_pages}는 측정에서 제외")

    # 페이지 간 크기 일관성 (1페이지 기준, 회전 동일시)
    ref_size = per_page[0][1]
    inconsistent = [i for i, size, _ in per_page[1:] if not _same_size(size, ref_size)]
    if inconsistent:
        notes.append(f"페이지 간 재단 크기 불일치 (첫 페이지 기준, 페이지 {inconsistent})")

    measured: dict[str, Any] = {
        "file_size_mm": [round(ref_size[0], 2), round(ref_size[1], 2)],
        "order_size_mm": None,
        "rotated": False,
    }
    if len(per_page) > 1:
        measured["page_sizes_mm"] = [[round(w, 2), round(h, 2)] for _, (w, h), _ in per_page]

    order_size = ctx.order.size_mm
    if order_size is None:
        # 주문 규격이 없으면 비교 생략 — 단, 페이지 간 크기 불일치는 파일 자체 결함이라 fail
        notes.append("주문 규격 미지정 — 비교 생략")
        status = CheckStatus.FAIL if inconsistent else CheckStatus.PASS
        return result(
            "page_size",
            status,
            measured=measured,
            required=required,
            pages=sorted(inconsistent),
            detail="; ".join(notes),
        )

    ow, oh = float(order_size[0]), float(order_size[1])
    measured["order_size_mm"] = [ow, oh]

    # 페이지별 주문 규격 대조 (정방향 또는 가로세로 교환 일치 허용)
    mismatch_pages: list[int] = []
    for i, (w, h), _ in per_page:
        direct = _near(w, ow) and _near(h, oh)
        swapped = _near(w, oh) and _near(h, ow)
        if not (direct or swapped):
            mismatch_pages.append(i)

    # 대표(첫) 페이지가 교환 일치로만 맞으면 회전 배치로 기록
    ref_direct = _near(ref_size[0], ow) and _near(ref_size[1], oh)
    ref_swapped = _near(ref_size[0], oh) and _near(ref_size[1], ow)
    if ref_swapped and not ref_direct:
        measured["rotated"] = True
        notes.append("가로세로가 주문과 뒤바뀐 상태로 일치 (회전 배치로 간주)")

    problem_pages = sorted(set(inconsistent) | set(mismatch_pages))
    if problem_pages:
        if per_page[0][0] in mismatch_pages:
            notes.append(
                f"재단 크기 {measured['file_size_mm'][0]}x{measured['file_size_mm'][1]}mm ≠ "
                f"주문 {ow:g}x{oh:g}mm (허용 ±{TOLERANCE_MM}mm)"
            )
        # 크기 불일치는 자동 보정 불가 — 재업로드/주문 변경 확인 질문 대상
        return result(
            "page_size",
            CheckStatus.FAIL,
            measured=measured,
            required=required,
            pages=problem_pages,
            detail="; ".join(notes),
        )

    notes.append("재단 크기 주문 규격과 일치")
    return result(
        "page_size",
        CheckStatus.PASS,
        measured=measured,
        required=required,
        detail="; ".join(notes),
    )
