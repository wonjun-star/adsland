"""resolution — 이미지 유효 해상도 검사 (§6 표).

콘텐츠 스트림의 ImageDraw 이벤트(픽셀 수 ÷ 배치 크기 = 유효 dpi)를
전 페이지에서 수집해 최저 해상도로 판정한다.

판정:
- 모든 이미지 ≥300dpi → pass
- 최저가 150~300dpi → warn (진행 가능하나 품질 고지)
- 최저가 150dpi 미만 → fail (육안 품질 저하 → 차단)
- 판정 대상 이미지가 없으면 → pass (detail에 명시)

배치 크기가 한 변이라도 5pt 미만인 이미지는 장식(아이콘·헤어라인 띠)으로
간주해 무시한다. 해상도는 이미 배치된 크기 기준이라 autofix 불가.
"""

from __future__ import annotations

from core.preflight.contentstream import ImageDraw
from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import CheckResult, CheckStatus

#: 이 값 이상이면 인쇄 품질 충분
PASS_DPI = 300.0
#: 이 값 미만이면 차단 (150~300 사이는 경고)
FAIL_BELOW_DPI = 150.0
#: 어느 한 변이라도 이보다 작게 배치된 이미지는 장식으로 보고 제외
MIN_PLACED_PT = 5.0
#: 부동소수점 경계 오차 흡수 (299.9999dpi 같은 노이즈)
EPS = 1e-6

_REQUIRED = {"pass_dpi": 300, "fail_below": 150}


@register_check("resolution")
def check_resolution(ctx: CheckContext) -> CheckResult:
    try:
        images: list[dict] = []       # measured용 (dpi는 표시용으로 반올림)
        raw_dpis: list[float] = []    # 판정용 원본 dpi (images와 인덱스 동일)
        skipped = 0                   # 장식/비정상으로 제외한 이미지 수
        problem_pages: set[int] = set()

        for page_i in range(ctx.page_count):
            for ev in ctx.content_events(page_i):
                if not isinstance(ev, ImageDraw):
                    continue
                # 장식 이미지(5pt 미만 배치) 또는 크기 정보가 깨진 이미지는 제외
                if min(ev.placed_w_pt, ev.placed_h_pt) < MIN_PLACED_PT:
                    skipped += 1
                    continue
                if ev.width_px <= 0 or ev.height_px <= 0:
                    skipped += 1
                    continue
                dpi = ev.effective_dpi
                raw_dpis.append(dpi)
                images.append({"page": page_i, "name": ev.name, "dpi": round(dpi, 1)})
                if dpi < PASS_DPI - EPS:
                    problem_pages.add(page_i)

        skip_note = f" (장식/비정상 이미지 {skipped}개 무시)" if skipped else ""

        if not images:
            return result(
                "resolution",
                CheckStatus.PASS,
                measured={"images": [], "min_dpi": None},
                required=_REQUIRED,
                detail="판정 대상 이미지 없음" + skip_note,
            )

        min_dpi = min(raw_dpis)
        if min_dpi >= PASS_DPI - EPS:
            status = CheckStatus.PASS
        elif min_dpi >= FAIL_BELOW_DPI - EPS:
            status = CheckStatus.WARN
        else:
            status = CheckStatus.FAIL

        return result(
            "resolution",
            status,
            measured={"images": images, "min_dpi": round(min_dpi, 1)},
            required=_REQUIRED,
            pages=sorted(problem_pages),
            detail=f"이미지 {len(images)}개 검사, 최저 유효 해상도 {min_dpi:.1f}dpi" + skip_note,
        )
    except Exception as e:  # 판정 불가 상황은 uncertain으로 격리 (예외 전파 금지)
        return result(
            "resolution",
            CheckStatus.UNCERTAIN,
            required=_REQUIRED,
            detail=f"해상도 측정 실패: {type(e).__name__}: {e}",
        )
