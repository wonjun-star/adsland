"""transparency — 투명도/오버프린트 탐지 (플래튼 필요 고지).

전 페이지 /Resources 를 순회하며 실제로 '투명한' 상태만 찾는다:
- ExtGState: /ca < 1, /CA < 1, /SMask 가 None(/None) 이외, /BM 이 Normal·Compatible 이외
- 이미지 XObject: /SMask 스트림 존재 (소프트 마스크 = 알파 채널)
- Form XObject 내부 /Resources 는 재귀 검사 (순환 방지)

중요: ExtGState '존재'만으로 판정하지 않는다 — 리셋용 /ca 1.0 딕셔너리는
정상이므로 무시한다. 값이 실제로 투명할 때만 warn.

발견 → warn ("인쇄 전 플래튼(flatten) 처리 필요" 고지 대상). 없으면 pass.
판단 불가(파일 손상 등) → uncertain.
"""

from __future__ import annotations

from typing import Any

import pikepdf

from core.preflight.engine import CheckContext, register_check, result
from core.preflight.report import CheckStatus

#: 불투명으로 간주하는 블렌드 모드 (PDF 스펙상 Compatible == Normal)
_OPAQUE_BLEND_MODES = {"Normal", "Compatible"}

#: Form XObject 재귀 깊이 제한 (contentstream 워커와 동일한 보수적 한계)
_MAX_DEPTH = 3


def _to_float(v: Any) -> float | None:
    """pikepdf 숫자(Decimal/int/float) → float. 실패 시 None."""
    try:
        return float(v)
    except Exception:
        return None


def _first_blend_mode(bm_obj: Any) -> str | None:
    """/BM 값 → 첫 블렌드 모드 이름. 배열이면 뷰어가 첫 항목을 쓰므로 첫 항목."""
    try:
        if isinstance(bm_obj, pikepdf.Array):
            if len(bm_obj) == 0:
                return None
            return str(bm_obj[0]).lstrip("/")
        return str(bm_obj).lstrip("/")
    except Exception:
        return None


def _smask_active(sm_obj: Any) -> bool:
    """ExtGState /SMask 값이 실제 소프트 마스크인지. /None 이름은 '마스크 없음'."""
    if sm_obj is None:
        return False
    if isinstance(sm_obj, pikepdf.Name):
        return str(sm_obj) != "/None"
    return True  # 딕셔너리(SMask dict)면 활성


def _scan_extgstate(egs: Any, page_i: int, found: list[dict], seen: set) -> None:
    """/ExtGState 딕셔너리에서 투명한 항목만 수집."""
    if not isinstance(egs, pikepdf.Dictionary):
        return
    for name, gd in egs.items():
        try:
            if not isinstance(gd, pikepdf.Dictionary):
                continue
            entry: dict[str, Any] = {"page": page_i, "name": str(name).lstrip("/")}
            transparent = False

            ca = _to_float(gd.get("/ca", None)) if "/ca" in gd else None
            if ca is not None:
                entry["ca"] = ca
                if ca < 1.0:
                    transparent = True

            CA = _to_float(gd.get("/CA", None)) if "/CA" in gd else None
            if CA is not None:
                entry["CA"] = CA
                if CA < 1.0:
                    transparent = True

            if "/BM" in gd:
                bm = _first_blend_mode(gd.get("/BM"))
                if bm is not None:
                    entry["bm"] = bm
                    if bm not in _OPAQUE_BLEND_MODES:
                        transparent = True

            if "/SMask" in gd and _smask_active(gd.get("/SMask")):
                entry["smask"] = True
                transparent = True

            if not transparent:
                continue  # 리셋용 /ca 1.0 등 불투명 상태는 무시

            key = (page_i, "extgstate", entry["name"])
            if key not in seen:
                seen.add(key)
                entry["kind"] = "extgstate"
                found.append(entry)
        except Exception:
            continue  # 개별 항목 해석 실패는 무시 (보수적)


def _scan_resources(res: Any, page_i: int, found: list[dict], seen: set,
                    visited: set, depth: int) -> None:
    """리소스 딕셔너리 1개 검사 + Form XObject 재귀."""
    if not isinstance(res, pikepdf.Dictionary):
        return
    # 간접 오브젝트 순환 방지
    try:
        if res.is_indirect:
            og = res.objgen
            if og in visited:
                return
            visited.add(og)
    except Exception:
        pass

    _scan_extgstate(res.get("/ExtGState", None), page_i, found, seen)

    xobjs = res.get("/XObject", None)
    if not isinstance(xobjs, pikepdf.Dictionary):
        return
    for name, xo in xobjs.items():
        try:
            subtype = str(xo.get("/Subtype", ""))
            if subtype == "/Image":
                # 이미지의 /SMask 스트림 = 알파 채널 → 투명
                if "/SMask" in xo and _smask_active(xo.get("/SMask")):
                    key = (page_i, "image_smask", str(name).lstrip("/"))
                    if key not in seen:
                        seen.add(key)
                        found.append({
                            "page": page_i,
                            "name": str(name).lstrip("/"),
                            "smask": True,
                            "kind": "image_smask",
                        })
            elif subtype == "/Form" and depth < _MAX_DEPTH:
                _scan_resources(xo.get("/Resources", None), page_i, found, seen,
                                visited, depth + 1)
        except Exception:
            continue


@register_check("transparency")
def check_transparency(ctx: CheckContext):
    """투명도 사용 여부 검사. 발견 시 warn (플래튼 필요 고지), 없으면 pass."""
    required = {
        "ca_min": 1.0,
        "CA_min": 1.0,
        "blend_modes": sorted(_OPAQUE_BLEND_MODES),
        "smask": False,
        "note": "발견 시 인쇄 전 플래튼(flatten) 처리 필요 고지",
    }
    try:
        found: list[dict] = []
        seen: set = set()
        for i in range(ctx.page_count):
            visited: set = set()
            page = ctx.pdf.pages[i]
            _scan_resources(page.get("/Resources", None), i, found, seen, visited, 0)

        pages = sorted({e["page"] for e in found})
        measured = {"transparent_states": found, "count": len(found)}
        if found:
            return result(
                "transparency",
                CheckStatus.WARN,
                measured=measured,
                required=required,
                pages=pages,
                detail=f"transparent states found: {len(found)} on pages {pages}",
            )
        return result(
            "transparency",
            CheckStatus.PASS,
            measured=measured,
            required=required,
            detail="no transparency (ca/CA<1, SMask, non-Normal BM) found",
        )
    except Exception as e:
        # 체크 내부에서 예외를 밖으로 내보내지 않는다 — 판단 불가는 uncertain
        return result(
            "transparency",
            CheckStatus.UNCERTAIN,
            required=required,
            detail=f"transparency scan failed: {type(e).__name__}: {e}",
        )
