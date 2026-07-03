"""black_type — 검정 텍스트는 먹1도(K only)인지 검사.

DeviceCMYK fill로 표시된 텍스트 중 '검정 의도'(K ≥ 0.9)인데
C+M+Y 성분이 섞인 것(리치블랙)을 찾는다. 본문 텍스트가 리치블랙이면
판 어긋남(핀 틀어짐) 시 글자가 번지고 잉크량도 불필요하게 커지므로 warn.

- render_mode 3(비표시 텍스트)은 무시
- DeviceGray 텍스트는 먹1도 취급 → 통과
- 다중 페이지 전부 검사, 위반 페이지는 pages에 기록 (0-base)
"""

from __future__ import annotations

from typing import Any

from core.preflight.contentstream import TextShow
from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import AutofixInfo, CheckResult, CheckStatus

#: '검정 의도' 판정: K 성분이 이 값 이상이면 검정 텍스트로 본다
K_INTENT_MIN = 0.9

#: 먹1도 허용 오차: C+M+Y 합이 이 값을 넘으면 리치블랙으로 판정
CMY_SUM_MAX = 0.05

#: measured에 담는 위반 색 목록 상한 (리포트 비대 방지 — 고유 색 기준)
MAX_LISTED = 50


@register_check("black_type")
def check_black_type(ctx: CheckContext) -> CheckResult:
    """전 페이지의 TextShow를 훑어 리치블랙 검정 텍스트를 찾는다."""
    rich_black_texts: list[dict[str, Any]] = []
    seen: set[tuple] = set()          # (page, cmyk) 중복 제거용
    total_events = 0                  # 위반 텍스트 표시 횟수 (중복 포함)
    bad_pages: list[int] = []

    try:
        for page_i in range(ctx.page_count):
            for ev in ctx.content_events(page_i):
                if not isinstance(ev, TextShow):
                    continue
                if ev.render_mode == 3:
                    # 보이지 않는 텍스트(예: OCR 레이어)는 인쇄에 영향 없음
                    continue
                color = ev.color
                if color.space != "DeviceCMYK" or len(color.components) != 4:
                    # DeviceGray 등 비CMYK 텍스트는 먹1도 취급 → 위반 아님
                    continue
                c, m, y, k = (float(v) for v in color.components)
                if k >= K_INTENT_MIN and (c + m + y) > CMY_SUM_MAX:
                    total_events += 1
                    if page_i not in bad_pages:
                        bad_pages.append(page_i)
                    cmyk = [round(c, 3), round(m, 3), round(y, 3), round(k, 3)]
                    key = (page_i, tuple(cmyk))
                    if key not in seen and len(rich_black_texts) < MAX_LISTED:
                        seen.add(key)
                        rich_black_texts.append({"page": page_i, "cmyk": cmyk})
    except Exception as e:
        # 콘텐츠 해석 자체가 불가능하면 판단 불가 → uncertain (예외 밖으로 내보내지 않음)
        return result(
            "black_type",
            CheckStatus.UNCERTAIN,
            required={"black_text": "0 0 0 1 k"},
            detail=f"콘텐츠 스트림 해석 실패: {type(e).__name__}: {e}",
        )

    status = CheckStatus.WARN if total_events > 0 else CheckStatus.PASS
    detail = (
        f"리치블랙 텍스트 {total_events}건 (고유 색 {len(rich_black_texts)}종, "
        f"기준: K≥{K_INTENT_MIN} 이면서 C+M+Y>{CMY_SUM_MAX})"
        if total_events
        else "검정 텍스트 전부 먹1도 (또는 검정 텍스트 없음)"
    )
    return result(
        "black_type",
        status,
        measured={"rich_black_texts": rich_black_texts},
        required={"black_text": "0 0 0 1 k"},
        autofix=AutofixInfo(available=False, note="본개발: K100 변환 예정"),
        pages=bad_pages,
        detail=detail,
    )
