"""colorspace — 색공간 검사: CMYK/그레이/별색(Separation)만 허용.

측정: 전 페이지의 콘텐츠 이벤트(ImageDraw.colorspace + VectorFill/VectorStroke/
TextShow 의 color.space)를 열거한다. 직접 파서를 만들지 않고 공유 워커
(ctx.content_events)의 정규화 문자열을 그대로 판독한다.

판정:
- RGB 계열(DeviceRGB / ICC-RGB / CalRGB / Indexed(RGB계))이 하나라도 → warn.
  fail 이 아닌 이유: RGB→CMYK 변환이 가능하므로 차단이 아니라 색변화 고지 대상.
- 전부 CMYK/그레이/별색 계열 → pass.
- 위 어느 쪽으로도 판별 불가한 색공간(Lab, Pattern, DeviceN 외 미지 이름 등)만
  발견되면 uncertain — 의도 판별이 필요해 에스컬레이션 대상.

한계(프로토타입): 텍스트는 fill 색만 기록되는 워커 한계를 따른다. 렌더 모드
3(비표시)·7(클립 전용) 텍스트는 인쇄에 나타나지 않으므로 색 판정에서 제외.
"""

from __future__ import annotations

from typing import Any

from core.preflight.contentstream import ImageDraw, TextShow, VectorFill, VectorStroke
from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import AutofixInfo, CheckResult, CheckStatus

CHECK_ID = "colorspace"

#: 기준값 — 고객 고지용 허용 색공간 대표 이름 (LLM 번역 입력)
ALLOWED = ["DeviceCMYK", "DeviceGray", "Separation"]

#: 허용 색공간의 정규화 문자열 (contentstream.colorspace_str 출력 기준).
#: ICC 기반이라도 채널 구성이 CMYK/그레이면 인쇄 가능으로 본다.
_ALLOWED_EXACT = {"DeviceCMYK", "DeviceGray", "ICC-CMYK", "ICC-Gray", "CalGray"}

#: 인쇄되지 않는 텍스트 렌더 모드 (3=비표시, 7=클립 전용)
_INVISIBLE_TEXT_MODES = {3, 7}

_AUTOFIX = AutofixInfo(available=False, note="본개발: ICC 기반 자동 변환(색변화 고지) 예정")


def _unwrap_indexed(space: str) -> str:
    """Indexed(...) 래퍼를 벗겨 베이스 색공간 문자열을 얻는다 (중첩 대비 반복)."""
    while space.startswith("Indexed(") and space.endswith(")"):
        space = space[len("Indexed(") : -1]
    return space


def _classify(space: str) -> str:
    """색공간 문자열 → 'rgb' | 'allowed' | 'unknown'."""
    base = _unwrap_indexed(space or "")
    # DeviceRGB / ICC-RGB / CalRGB / Indexed(…RGB…) 전부 포괄
    if "RGB" in base.upper():
        return "rgb"
    if base in _ALLOWED_EXACT or base.startswith("Separation"):
        return "allowed"
    return "unknown"


def _event_kind_space(ev: Any) -> tuple[str, str] | None:
    """이벤트 → (kind, 색공간 문자열). 판정 제외 대상이면 None."""
    if isinstance(ev, ImageDraw):
        return ("image", ev.colorspace)
    if isinstance(ev, VectorFill):
        return ("fill", ev.color.space)
    if isinstance(ev, VectorStroke):
        return ("stroke", ev.color.space)
    if isinstance(ev, TextShow):
        if ev.render_mode in _INVISIBLE_TEXT_MODES:
            return None  # 인쇄에 나타나지 않는 텍스트는 색 무관
        return ("text", ev.color.space)
    return None


@register_check(CHECK_ID)
def check_colorspace(ctx: CheckContext) -> CheckResult:
    rgb_objects: list[dict[str, Any]] = []      # 스펙 형식: {"kind","page","space"} (중복 제거)
    unknown_objects: list[dict[str, Any]] = []  # 판별 불가 색공간
    spaces_seen: set[str] = set()
    error_pages: list[int] = []                 # 이벤트 해석 자체가 실패한 페이지
    rgb_draw_total = 0                          # 중복 제거 전 RGB 사용 횟수

    try:
        page_count = ctx.page_count
    except Exception as e:  # PDF 자체를 열 수 없음 → 판단 불가
        return result(
            CHECK_ID,
            CheckStatus.UNCERTAIN,
            required={"allowed": ALLOWED},
            autofix=_AUTOFIX.model_copy(),
            detail=f"PDF 열기 실패로 색공간 판정 불가: {type(e).__name__}: {e}",
        )

    for page_i in range(page_count):
        try:
            events = ctx.content_events(page_i)
        except Exception:  # 페이지 단위 격리 — 나머지 페이지는 계속 검사
            error_pages.append(page_i)
            continue
        for ev in events:
            ks = _event_kind_space(ev)
            if ks is None:
                continue
            kind, space = ks
            spaces_seen.add(space)
            cls = _classify(space)
            if cls == "allowed":
                continue
            entry = {"kind": kind, "page": page_i, "space": space}
            if cls == "rgb":
                rgb_draw_total += 1
                if entry not in rgb_objects:
                    rgb_objects.append(entry)
            else:  # unknown
                if entry not in unknown_objects:
                    unknown_objects.append(entry)

    measured: dict[str, Any] = {
        "rgb_objects": rgb_objects,
        "unknown_objects": unknown_objects,
        "spaces_seen": sorted(spaces_seen),
        "rgb_draw_total": rgb_draw_total,
    }
    required = {"allowed": ALLOWED}

    if rgb_objects:
        pages = sorted({o["page"] for o in rgb_objects})
        return result(
            CHECK_ID,
            CheckStatus.WARN,
            measured=measured,
            required=required,
            autofix=_AUTOFIX.model_copy(),
            pages=pages,
            detail=(
                f"RGB 계열 색공간 {len(rgb_objects)}종(사용 {rgb_draw_total}회) 검출 — "
                f"페이지 {pages}. CMYK 변환 시 색상 변화 고지 필요."
            ),
        )

    if unknown_objects or error_pages:
        pages = sorted({o["page"] for o in unknown_objects} | set(error_pages))
        why = []
        if unknown_objects:
            why.append(f"판별 불가 색공간 {len(unknown_objects)}종: "
                       + ", ".join(sorted({o['space'] for o in unknown_objects})))
        if error_pages:
            why.append(f"이벤트 해석 실패 페이지: {error_pages}")
        return result(
            CHECK_ID,
            CheckStatus.UNCERTAIN,
            measured=measured,
            required=required,
            autofix=_AUTOFIX.model_copy(),
            pages=pages,
            detail="; ".join(why),
        )

    return result(
        CHECK_ID,
        CheckStatus.PASS,
        measured=measured,
        required=required,
        autofix=_AUTOFIX.model_copy(),
        detail=f"전 페이지({page_count}p) 색공간 허용 범위 내: {sorted(spaces_seen)}",
    )
