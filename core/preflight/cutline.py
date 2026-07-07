"""칼선(도무송) 파일 검증 — 도무송 주문의 '칼선 별도 파일'을 검사한다.

애즈랜드 도무송 접수 방식: 인쇄 도안과 별개로 **칼선(K100) 파일**을 따로 낸다. 이 모듈은
그 칼선 파일이 도무송 규칙(DIELINE_RULES)에 맞는지 본다 — 칼선(벡터 선)이 있는지, 크기가
최소치(10mm) 이상인지. (색상 K100 여부는 권장이나, 실물 샘플 확보 전까지는 강제하지 않는다.)

한계(정직): 애즈랜드 실제 칼선 파일 샘플을 아직 확보하지 못해, '칼선 존재 + 최소 크기'
수준의 휴리스틱 검증이다. 칼선 모서리 3R·개체 간 3mm 같은 정밀 규칙은 샘플 확보 후 강화한다.
"""

from __future__ import annotations

from pathlib import Path

from core.preflight.adsland_guide import DIELINE_RULES
from core.preflight.contentstream import VectorStroke
from core.preflight.engine import CheckContext

_PT_TO_MM = 25.4 / 72.0


def validate_cutline(pdf_path: str | Path) -> dict:
    """칼선 파일 검사. 반환: {ok, reasons, size_mm, stroke_count}."""
    ctx = CheckContext(str(pdf_path))
    bboxes: list[tuple[float, float, float, float]] = []
    try:
        for pi in range(ctx.page_count):
            for ev in ctx.content_events(pi):
                if isinstance(ev, VectorStroke) and ev.bbox:
                    bboxes.append(ev.bbox)
    except Exception as e:
        return {"ok": False, "reasons": [f"칼선 파일을 해석하지 못했어요: {type(e).__name__}"],
                "size_mm": None, "stroke_count": 0}
    finally:
        ctx.close()

    if not bboxes:
        return {
            "ok": False,
            "reasons": ["칼선(벡터 선)이 없어요 — 칼선을 K100 선으로 그려서 올려 주세요."],
            "size_mm": None,
            "stroke_count": 0,
        }

    x0 = min(b[0] for b in bboxes)
    y0 = min(b[1] for b in bboxes)
    x1 = max(b[2] for b in bboxes)
    y1 = max(b[3] for b in bboxes)
    w_mm = (x1 - x0) * _PT_TO_MM
    h_mm = (y1 - y0) * _PT_TO_MM
    size_mm = (round(w_mm, 1), round(h_mm, 1))

    reasons: list[str] = []
    min_mm = float(DIELINE_RULES["min_size_mm"])
    if min(w_mm, h_mm) + 1e-6 < min_mm:
        reasons.append(
            f"칼선 크기가 최소 {min_mm:g}mm보다 작아요 (현재 {size_mm[0]}×{size_mm[1]}mm)."
        )

    return {"ok": not reasons, "reasons": reasons, "size_mm": size_mm, "stroke_count": len(bboxes)}
