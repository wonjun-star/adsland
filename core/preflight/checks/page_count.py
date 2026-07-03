"""page_count — 파일 페이지 수가 주문 페이지 수와 일치하는지 (§6 표: 페이지 트리).

측정은 페이지 트리 길이(ctx.page_count) 하나뿐이다. 불일치 시 파일과 주문 중
어느 쪽이 고객 의도인지 기계가 판단할 수 없으므로 autofix 불가 → fail(질문 대상).
"""

from __future__ import annotations

from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import CheckResult, CheckStatus

CHECK_ID = "page_count"


@register_check(CHECK_ID)
def check_page_count(ctx: CheckContext) -> CheckResult:
    # 파일 페이지 수 실측 — PDF 자체가 깨져 열리지 않으면 판단 불가
    try:
        file_pages = ctx.page_count
    except Exception as e:
        return result(
            CHECK_ID,
            CheckStatus.UNCERTAIN,
            detail=f"페이지 수 측정 실패: {type(e).__name__}: {e}",
        )

    order_pages = ctx.order.page_count if ctx.order is not None else None
    measured = {"file_pages": file_pages, "order_pages": order_pages}

    # 주문에 페이지 수가 없으면 비교 기준 자체가 없다 → pass (주문 미지정)
    if order_pages is None:
        return result(
            CHECK_ID,
            CheckStatus.PASS,
            measured=measured,
            detail="주문 미지정 — 페이지 수 비교 생략",
        )

    required = {"page_count": order_pages}

    if file_pages == order_pages:
        return result(
            CHECK_ID,
            CheckStatus.PASS,
            measured=measured,
            required=required,
        )

    # 불일치 — autofix 불가(고객 확인 질문 대상).
    # 파일이 주문보다 많으면 초과분 페이지를 문제 페이지로 기록 (0-base).
    pages = list(range(order_pages, file_pages)) if file_pages > order_pages else []
    return result(
        CHECK_ID,
        CheckStatus.FAIL,
        measured=measured,
        required=required,
        pages=pages,
        detail=f"파일 {file_pages}p ≠ 주문 {order_pages}p",
    )
