"""font_embed — 사용된 폰트 전체 임베딩 검사 (PLAN §6).

미임베딩 폰트가 있으면 RIP 단계에서 글꼴이 치환되어 인쇄물이 시안과 달라진다.
사용된 폰트 중 1개라도 미임베딩이면 fail, 전부 임베딩이면 pass, 텍스트가 없어도 pass.

측정 방법:
- '사용' 판정: ctx.content_events()의 TextShow.font(리소스 키) 집합과 대조.
  리소스 /Font 에 있어도 실제 표시에 쓰이지 않는 폰트는 무시한다
  (reportlab 등이 페이지 프리앰블에 기본 Helvetica를 끼워넣는 경우가 실무에 흔함).
- 임베딩 판정: Type0 → DescendantFonts[0]의 FontDescriptor,
  Type1/TrueType 등 → 자신의 FontDescriptor. FontFile|FontFile2|FontFile3 존재 여부.
  Type3는 글리프가 CharProcs로 문서에 내장되므로 임베딩으로 취급.
- base-14 표준폰트(예: /Helvetica)는 FontDescriptor 자체가 없다 → 미임베딩.

판정 불가(콘텐츠가 참조하는 폰트 키를 리소스에서 못 찾는 등)는 uncertain으로
격리한다 — 예외를 밖으로 던지지 않는다.
"""

from __future__ import annotations

from typing import Any

from core.preflight.contentstream import TextShow
from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import AutofixInfo, CheckResult, CheckStatus

CHECK_ID = "font_embed"

#: 폰트 프로그램이 담기는 FontDescriptor 키 (Type1 / TrueType / CFF·OpenType)
_FONT_FILE_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")

#: contentstream._walk와 같은 Form XObject 재귀 깊이 — 이벤트에 나온 키는 이 범위 안에 있다
_MAX_FORM_DEPTH = 2


def _font_descriptor(font_obj: Any):
    """폰트 오브젝트 → FontDescriptor. Type0은 DescendantFonts[0] 경유. 없으면 None."""
    subtype = str(font_obj.get("/Subtype", ""))
    if subtype == "/Type0":
        desc = font_obj.get("/DescendantFonts")
        if desc is None or len(desc) == 0:
            return None
        return desc[0].get("/FontDescriptor")
    return font_obj.get("/FontDescriptor")


def _is_embedded(font_obj: Any) -> bool | None:
    """임베딩 여부. Type3=True(글리프 내장). 오브젝트가 깨져 판정 불가면 None."""
    try:
        if str(font_obj.get("/Subtype", "")) == "/Type3":
            return True
        fd = _font_descriptor(font_obj)
        if fd is None:
            return False  # base-14 표준폰트 등 — FontDescriptor 자체가 없음
        return any(k in fd for k in _FONT_FILE_KEYS)
    except Exception:
        return None


def _base_font_name(font_obj: Any, fallback_key: str) -> str:
    """/BaseFont 이름 (예: '/Helvetica', '/ABCDEE+Vera'). 없으면 리소스 키로 폴백."""
    try:
        bf = font_obj.get("/BaseFont")
        if bf is not None:
            return str(bf)
    except Exception:
        pass
    return fallback_key


def _collect_fonts(resources: Any, out: dict[str, list], depth: int = 0) -> None:
    """리소스의 /Font 항목 수집 (키 → 폰트 오브젝트 목록).

    Form XObject 내부 리소스도 contentstream._walk와 같은 깊이(2)까지 훑는다 —
    TextShow.font 키가 폼 내부 리소스에서 온 경우까지 커버.
    """
    if resources is None:
        return
    try:
        fonts = resources.get("/Font")
    except Exception:
        fonts = None
    if fonts is not None:
        try:
            for name, obj in fonts.items():
                out.setdefault(str(name), []).append(obj)
        except Exception:
            pass
    if depth >= _MAX_FORM_DEPTH:
        return
    try:
        xobjs = resources.get("/XObject")
        items = list(xobjs.items()) if xobjs is not None else []
    except Exception:
        return
    for _name, xo in items:
        try:
            if str(xo.get("/Subtype", "")) == "/Form":
                inner = xo.get("/Resources")
                if inner is not None:
                    _collect_fonts(inner, out, depth + 1)
        except Exception:
            continue


@register_check(CHECK_ID)
def check_font_embed(ctx: CheckContext) -> CheckResult:
    """전 페이지의 '사용된' 폰트가 모두 임베딩(또는 Type3 내장)인지 검사."""
    autofix = AutofixInfo(available=False, note="본개발: 아웃라인화 지원 예정")
    try:
        # (이름, 임베딩상태) → measured 엔트리. 같은 폰트가 여러 페이지에 나와도 1건.
        entries: dict[tuple[str, bool | None], dict[str, Any]] = {}
        fail_pages: set[int] = set()      # 미임베딩 폰트가 '사용'된 페이지
        unknown_pages: set[int] = set()   # 판정 불가 폰트가 사용된 페이지

        def _record(name: str, embedded: bool | None, used: bool) -> None:
            e = entries.setdefault((name, embedded), {"name": name, "embedded": embedded, "used": False})
            e["used"] = e["used"] or used

        for page_i in range(ctx.page_count):
            # 1) 이 페이지에서 실제 텍스트 표시에 쓰인 폰트 리소스 키
            used_keys = {
                ev.font for ev in ctx.content_events(page_i) if isinstance(ev, TextShow) and ev.font
            }
            # 2) 페이지(+Form XObject) 리소스의 폰트 사전
            key_to_fonts: dict[str, list] = {}
            _collect_fonts(ctx.resources(page_i), key_to_fonts)

            # 3) 사용된 키별 임베딩 대조
            for key in sorted(used_keys):
                candidates = key_to_fonts.get(key)
                if not candidates:
                    # 콘텐츠는 참조하는데 리소스에서 폰트를 못 찾음 → 판정 불가
                    _record(key, None, used=True)
                    unknown_pages.add(page_i)
                    continue
                for font_obj in candidates:
                    emb = _is_embedded(font_obj)
                    _record(_base_font_name(font_obj, key), emb, used=True)
                    if emb is False:
                        fail_pages.add(page_i)
                    elif emb is None:
                        unknown_pages.add(page_i)

            # 4) 리소스에 있지만 안 쓰는 폰트 — 판정에서 제외하되 measured에는 남긴다
            for key, candidates in key_to_fonts.items():
                if key in used_keys:
                    continue
                for font_obj in candidates:
                    _record(_base_font_name(font_obj, key), _is_embedded(font_obj), used=False)

        # 미임베딩·사용 폰트 먼저 → LLM이 문제 폰트를 바로 짚을 수 있게 정렬
        fonts_list = sorted(
            entries.values(),
            key=lambda f: (f["embedded"] is not False, not f["used"], f["name"]),
        )
        used_fonts = [f for f in fonts_list if f["used"]]
        bad_names = [f["name"] for f in used_fonts if f["embedded"] is False]
        unknown_names = [f["name"] for f in used_fonts if f["embedded"] is None]

        measured = {
            "fonts": fonts_list,
            "used_font_count": len(used_fonts),
            "unembedded_used_font_count": len(bad_names),
            "unembedded_used_fonts": bad_names,
        }
        required = {"unembedded_used_font_count": 0}

        if bad_names:
            return result(
                CHECK_ID,
                CheckStatus.FAIL,
                measured=measured,
                required=required,
                pages=sorted(fail_pages),
                autofix=autofix,
                detail=f"미임베딩 사용 폰트 {len(bad_names)}종: {', '.join(bad_names)}",
            )
        if unknown_names:
            return result(
                CHECK_ID,
                CheckStatus.UNCERTAIN,
                measured=measured,
                required=required,
                pages=sorted(unknown_pages),
                autofix=autofix,
                detail=f"임베딩 판정 불가 폰트: {', '.join(unknown_names)} (리소스 미해결/오브젝트 손상)",
            )
        return result(
            CHECK_ID,
            CheckStatus.PASS,
            measured=measured,
            required=required,
            autofix=autofix,
            detail="사용 폰트 전부 임베딩" if used_fonts else "표시 텍스트 없음",
        )
    except Exception as e:  # 체크 내부 오류는 밖으로 던지지 않는다
        return result(
            CHECK_ID,
            CheckStatus.UNCERTAIN,
            autofix=autofix,
            detail=f"검사 실패: {type(e).__name__}: {e}",
        )
