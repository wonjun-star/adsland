"""dieline — 칼선(별색 CutContour 등) 존재 검사.

측정: 전 페이지에서 Separation 별색 이름을 열거한다.
  1) 페이지 리소스 /ColorSpace 의 [/Separation /이름 ...] (Form XObject 리소스 2단계 재귀 포함)
  2) 콘텐츠 이벤트(VectorStroke/VectorFill/TextShow/ImageDraw)의 "Separation:이름" 색공간

판정:
  - 주문 상품이 sticker/label(칼선 상품)인데 칼선 별색 없음 → uncertain (자유형 재단 여부 질문 대상)
  - 칼선 상품이고 칼선 있음 → pass (measured.dieline_present=true — 오케스트레이터가 cut_type 추론에 사용)
  - 그 외 상품인데 칼선 있음 → uncertain (의도 확인)
  - 주문 상품 미상(None) → pass + 존재 여부만 기록
"""

from __future__ import annotations

import re

from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import CheckResult, CheckStatus

#: 칼선으로 간주하는 별색 이름 패턴 (대소문자 무시). 업계 관례 이름들.
_DIELINE_PATTERN = r"cutcontour|thru.?cut|kiss.?cut|dieline|die|cut"
_DIELINE_RE = re.compile(_DIELINE_PATTERN, re.IGNORECASE)

#: 이벤트 색공간 문자열에서 Separation 이름 추출 ("Separation:이름", "Indexed(Separation:이름)" 등)
_EVENT_SEP_RE = re.compile(r"Separation:([^)]+)")

#: 칼선이 반드시 있어야 하는 상품
_DIELINE_PRODUCTS = {"sticker", "label"}


def _spots_from_resources(res, depth: int = 0) -> set[str]:
    """리소스 딕셔너리의 /ColorSpace 에서 Separation 별색 이름 수집.

    칼선이 Form XObject 안에만 정의된 경우를 위해 /XObject → /Resources 를 2단계까지 재귀.
    개별 항목 해석 실패는 무시한다 (보수적).
    """
    import pikepdf

    names: set[str] = set()
    if res is None:
        return names

    try:
        csd = res.get("/ColorSpace")
    except Exception:
        csd = None
    if csd is not None:
        try:
            items = list(csd.items())
        except Exception:
            items = []
        for _key, cs in items:
            try:
                if isinstance(cs, pikepdf.Array) and len(cs) >= 2 and str(cs[0]) == "/Separation":
                    names.add(str(cs[1]).lstrip("/"))
            except Exception:
                continue

    if depth < 2:
        try:
            xobjs = res.get("/XObject")
        except Exception:
            xobjs = None
        if xobjs is not None:
            try:
                values = [v for _k, v in xobjs.items()]
            except Exception:
                values = []
            for xo in values:
                try:
                    if str(xo.get("/Subtype", "")) == "/Form":
                        names |= _spots_from_resources(xo.get("/Resources"), depth + 1)
                except Exception:
                    continue
    return names


def _spots_from_events(events) -> set[str]:
    """콘텐츠 이벤트의 색공간 문자열에서 Separation 별색 이름 수집."""
    names: set[str] = set()
    for ev in events:
        spaces: list[str] = []
        color = getattr(ev, "color", None)  # VectorFill / VectorStroke / TextShow
        if color is not None:
            space = getattr(color, "space", "")
            if isinstance(space, str):
                spaces.append(space)
        cs = getattr(ev, "colorspace", None)  # ImageDraw
        if isinstance(cs, str):
            spaces.append(cs)
        for s in spaces:
            for m in _EVENT_SEP_RE.finditer(s):
                names.add(m.group(1).strip().lstrip("/"))
    return names


@register_check("dieline")
def check_dieline(ctx: CheckContext) -> CheckResult:
    """칼선 별색 존재 검사. 예외는 밖으로 내보내지 않고 uncertain으로 처리."""
    try:
        n_pages = ctx.page_count
    except Exception as e:
        return result(
            "dieline",
            CheckStatus.UNCERTAIN,
            detail=f"PDF 페이지 열람 실패: {type(e).__name__}: {e}",
        )

    try:
        # 페이지별 Separation 별색 이름 수집 (리소스 + 이벤트)
        per_page: dict[int, set[str]] = {}
        for i in range(n_pages):
            names: set[str] = set()
            try:
                names |= _spots_from_resources(ctx.resources(i))
            except Exception:
                pass
            try:
                names |= _spots_from_events(ctx.content_events(i))
            except Exception:
                pass
            per_page[i] = names

        all_spots = sorted(set().union(*per_page.values())) if per_page else []
        dieline_names = sorted({n for n in all_spots if _DIELINE_RE.search(n)})
        pages_with_dieline = sorted(
            i for i, ns in per_page.items() if any(_DIELINE_RE.search(n) for n in ns)
        )
        present = bool(dieline_names)

        product = (ctx.order.product or "").strip().lower() if ctx.order else ""
        requires = product in _DIELINE_PRODUCTS

        measured = {
            "dieline_present": present,
            "spot_names": all_spots,
            "dieline_spot_names": dieline_names,
        }
        required = {
            "dieline_required": requires if product else None,
            "name_pattern": _DIELINE_PATTERN,
        }

        if not product:
            # 주문 정보 없음 → 존재 여부만 기록하고 통과
            return result(
                "dieline",
                CheckStatus.PASS,
                measured=measured,
                required=required,
                detail=f"주문 상품 미상 — 칼선 별색 {'있음' if present else '없음'} (spot={all_spots})",
            )

        if requires:
            if present:
                return result(
                    "dieline",
                    CheckStatus.PASS,
                    measured=measured,
                    required=required,
                    detail=(
                        f"칼선 별색 발견: {', '.join(dieline_names)}"
                        f" (페이지 {pages_with_dieline})"
                    ),
                )
            # 칼선 상품인데 칼선 없음 → 재단 형태(cut_type)에 따라 판정.
            # 이건 고객이 정할 문제이므로, 재단 형태를 알면 바로 판정하고 사람에게 넘기지 않는다.
            cut_type = (getattr(ctx.order, "cut_type", None) or "").strip().lower() if ctx.order else ""
            if cut_type in ("square", "circle"):
                return result(
                    "dieline",
                    CheckStatus.PASS,
                    measured=measured,
                    required=required,
                    detail=f"칼선 없음 — {cut_type} 재단이라 칼선 불필요",
                )
            if cut_type == "die_cut":
                # 칼선 파일을 별도로 받아 검증 통과했으면(애즈랜드 4파일 분리 접수 방식) 통과.
                if getattr(ctx.order, "has_cutline", False):
                    measured["cutline_file"] = True
                    return result(
                        "dieline",
                        CheckStatus.PASS,
                        measured=measured,
                        required=required,
                        detail="도무송 — 칼선 파일이 별도로 제공되어 검증 통과",
                    )
                return result(
                    "dieline",
                    CheckStatus.FAIL,
                    measured=measured,
                    required=required,
                    pages=list(range(n_pages)),
                    detail="도무송(자유형) 재단인데 칼선이 없음 — 칼선(K100) 파일을 따로 올려주세요",
                )
            # 재단 형태 미정 → 고객에게 물어볼 대상 (사람 검판 아님)
            return result(
                "dieline",
                CheckStatus.UNCERTAIN,
                measured=measured,
                required=required,
                pages=list(range(n_pages)),
                detail=(
                    f"'{product}' 주문인데 칼선 별색 없음 — 사각 재단인지 자유형(칼선 필요)인지 고객 확인"
                    f" (발견 별색: {all_spots if all_spots else '없음'})"
                ),
            )

        # 칼선 불필요 상품
        if present:
            return result(
                "dieline",
                CheckStatus.UNCERTAIN,
                measured=measured,
                required=required,
                pages=pages_with_dieline,
                detail=(
                    f"'{product}' 주문인데 칼선 별색 존재: {', '.join(dieline_names)}"
                    " — 특수 재단 의도인지 확인 필요"
                ),
            )
        return result(
            "dieline",
            CheckStatus.PASS,
            measured=measured,
            required=required,
            detail="칼선 별색 없음 (해당 상품은 칼선 불필요)",
        )
    except Exception as e:  # 판단 불가 상황은 uncertain으로 격리
        return result(
            "dieline",
            CheckStatus.UNCERTAIN,
            detail=f"검사 중 오류: {type(e).__name__}: {e}",
        )
