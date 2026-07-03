"""min_line — 최소 선굵기 검사 (≥0.25pt).

인쇄기에서 0.25pt 미만의 선(특히 0pt 헤어라인)은 끊기거나 아예 안 나올 수 있다.
콘텐츠 스트림의 VectorStroke 이벤트(CTM 반영 유효 선폭)를 전 페이지 수집해서
0.25pt 미만 스트로크가 있으면 warn.

제외 대상: Separation(별색) 스트로크 — 칼선(CutContour 등)은 인쇄되는 선이
아니라 재단 장비용 가이드이므로 선굵기 기준을 적용하지 않는다.
"""

from __future__ import annotations

from core.preflight.contentstream import VectorStroke
from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import CheckResult, CheckStatus

#: 인쇄 안전 최소 선굵기 (pt)
MIN_LINE_PT = 0.25

#: 부동소수점 오차 여유 — 정확히 0.25pt인 선은 통과시킨다
_EPS = 1e-6

#: measured에 담는 얇은 선 목록 상한 (리포트 비대 방지)
_MAX_LISTED = 20


@register_check("min_line")
def check_min_line(ctx: CheckContext) -> CheckResult:
    """전 페이지 스트로크 유효 폭을 검사해 0.25pt 미만(0 포함)이 있으면 warn."""
    try:
        thin: list[dict] = []          # 기준 미달 스트로크 [{"page", "width_pt"}]
        widths: list[float] = []       # 인쇄되는 스트로크 전체 폭 (min 계산용)
        pages_bad: set[int] = set()

        for page_i in range(ctx.page_count):
            for ev in ctx.content_events(page_i):
                # VectorStroke 이벤트만 대상
                if not isinstance(ev, VectorStroke):
                    continue
                # 별색(칼선) 스트로크는 인쇄되지 않으므로 제외
                if ev.color.space.startswith("Separation"):
                    continue
                w = float(ev.line_width_pt)
                widths.append(w)
                if w < MIN_LINE_PT - _EPS:
                    thin.append({"page": page_i, "width_pt": round(w, 4)})
                    pages_bad.add(page_i)

        measured = {
            # 인쇄 스트로크가 하나도 없으면 None (측정 대상 없음 = 문제 없음)
            "min_width_pt": round(min(widths), 4) if widths else None,
            "thin_strokes": thin[:_MAX_LISTED],
            "thin_stroke_count": len(thin),
            "stroke_count": len(widths),
        }
        required = {"min_pt": MIN_LINE_PT}

        if thin:
            return result(
                "min_line",
                CheckStatus.WARN,
                measured=measured,
                required=required,
                pages=sorted(pages_bad),
                detail=(
                    f"{len(thin)} stroke(s) below {MIN_LINE_PT}pt; "
                    f"min={measured['min_width_pt']}pt"
                ),
            )
        return result(
            "min_line",
            CheckStatus.PASS,
            measured=measured,
            required=required,
            detail=(
                "no printing strokes"
                if not widths
                else f"all {len(widths)} stroke(s) >= {MIN_LINE_PT}pt (min={measured['min_width_pt']}pt)"
            ),
        )
    except Exception as e:  # 판단 불가 상황은 uncertain으로 격리 (예외 전파 금지)
        return result(
            "min_line",
            CheckStatus.UNCERTAIN,
            required={"min_pt": MIN_LINE_PT},
            detail=f"min_line check error: {type(e).__name__}: {e}",
        )
