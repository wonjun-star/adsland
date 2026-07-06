"""ink_total — 총잉크량(TAC) ≤300% 검사.

콘텐츠 스트림 이벤트(VectorFill / VectorStroke / TextShow) 가운데
DeviceCMYK 색을 쓰는 것들의 잉크 총량(C+M+Y+K 합, %) 최대값을 전 페이지에서 측정한다.

중요: 렌더 비트맵은 RGB로 합성되므로 왕복 변환으로는 300% 초과를 복원할 수 없다.
반드시 콘텐츠 스트림의 원본 CMYK 성분으로 측정해야 한다.

프로토타입 한계: 이미지 픽셀 단위 잉크량은 측정하지 않는다 — 이미지 제외(벡터만).
합성 코퍼스의 사진 최대 잉크량은 약 247%라 300% 임계에 걸리지 않아 안전하다.
"""

from __future__ import annotations

from core.preflight.contentstream import TextShow, VectorFill, VectorStroke
from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import AutofixInfo, CheckResult, CheckStatus

#: 총잉크량 상한 (%) — PLAN §6: ink_total ≤300%
MAX_INK_PERCENT = 300.0
#: 부동소수점·반올림 허용 오차 (%p) — 300.5 이하까지는 pass
TOLERANCE = 0.5
#: 리포트에 담을 초과 객체 최대 개수 (measured 비대화 방지, 총 개수는 over_count로 보존)
MAX_OVER_OBJECTS = 20

#: 이벤트 타입 → 리포트용 종류 문자열 (ImageDraw는 의도적으로 제외)
_KIND = {VectorFill: "fill", VectorStroke: "stroke", TextShow: "text"}


@register_check("ink_total")
def check_ink_total(ctx: CheckContext) -> CheckResult:
    """전 페이지의 벡터·텍스트 DeviceCMYK 잉크 합 최대값을 재고 상한 초과면 warn.

    상한은 애즈랜드 가이드 품목별: 전단·포스터·리플렛 250%, 그 외 300%.
    """
    max_percent = (
        ctx.order.max_ink_percent
        if (ctx.order and ctx.order.max_ink_percent is not None)
        else MAX_INK_PERCENT
    )
    required = {"max_percent": round(max_percent)}
    autofix = AutofixInfo(available=False, note="본개발: 잉크 리미팅 예정")

    try:
        max_ink = 0.0
        over_objects: list[dict] = []
        over_count = 0
        bad_pages: set[int] = set()

        for page_i in range(ctx.page_count):
            for ev in ctx.content_events(page_i):
                kind = _KIND.get(type(ev))
                if kind is None:
                    # ImageDraw 등 — 이미지 픽셀 잉크량은 프로토타입 범위 밖
                    continue
                if isinstance(ev, TextShow) and ev.render_mode == 3:
                    # 보이지 않는 텍스트(Tr 3)는 잉크를 올리지 않는다
                    continue
                pct = ev.color.cmyk_sum_percent
                if pct is None:
                    # DeviceCMYK 외 색공간(RGB·Gray·별색 등)은 잉크 합 산출 대상 아님
                    continue
                if pct > max_ink:
                    max_ink = pct
                if pct > max_percent + TOLERANCE:
                    over_count += 1
                    bad_pages.add(page_i)
                    if len(over_objects) < MAX_OVER_OBJECTS:
                        over_objects.append(
                            {"kind": kind, "page": page_i, "percent": round(pct, 1)}
                        )
    except Exception as e:
        # 판단 불가(파일 열기·스트림 해석 실패 등)는 uncertain으로 격리
        return result(
            "ink_total",
            CheckStatus.UNCERTAIN,
            required=required,
            autofix=autofix,
            detail=f"콘텐츠 스트림 해석 실패: {type(e).__name__}: {e}",
        )

    measured = {
        "max_ink_percent": round(max_ink, 1),
        "over_objects": over_objects,
        "over_count": over_count,
    }
    status = (
        CheckStatus.PASS if max_ink <= max_percent + TOLERANCE else CheckStatus.WARN
    )
    detail = (
        f"벡터·텍스트 DeviceCMYK 잉크 합 최대 {max_ink:.1f}% "
        f"(기준 ≤{max_percent:.0f}%, +{TOLERANCE}%p 허용). "
        f"이미지 제외(벡터만) — 이미지 픽셀 잉크량은 프로토타입 범위 밖."
    )
    return result(
        "ink_total",
        status,
        measured=measured,
        required=required,
        autofix=autofix,
        pages=sorted(bad_pages),
        detail=detail,
    )
