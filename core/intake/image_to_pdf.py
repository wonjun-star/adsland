"""JPG/PNG 접수 — 이미지를 PDF로 감싸 검수 파이프라인에 태운다.

애즈랜드는 JPG/PNG도 받는다(캔바·미리캔버스 등은 고해상 JPG로 접수 권장). 우리 검수는
PDF 기준이므로, 올라온 이미지를 **주문 규격 크기의 1페이지 PDF**로 감싼다. 규격을 알면
그 크기로(→ 해상도 검사가 실제 dpi를 잰다), 모르면 이미지 dpi(없으면 300)로 페이지를 잡는다.

한계(정직): 이미지는 래스터라 벡터 검수(선굵기·별색 칼선·글꼴)는 대상이 없다 —
그 항목들은 '해당 없음'으로 통과하고, 이미지엔 해상도·크기 검수만 유효하다.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import numpy as np
import pikepdf
from PIL import Image

#: 이미지 매직바이트
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def is_image_bytes(data: bytes) -> str | None:
    """바이트 앞부분으로 이미지 형식 판별 → 'png'|'jpeg'|None."""
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(JPEG_MAGIC):
        return "jpeg"
    return None


def image_to_pdf(
    img_path: str | Path,
    out_pdf: str | Path,
    size_mm: tuple[float, float] | None = None,
) -> dict:
    """이미지 → 1페이지 PDF. size_mm를 주면 그 크기로, 없으면 이미지 dpi(기본 300)로.

    반환: {out_path, w_px, h_px, page_mm}
    """
    img_path, out_pdf = Path(img_path), Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    im = Image.open(img_path).convert("RGB")
    w_px, h_px = im.size

    if size_mm and size_mm[0] and size_mm[1]:
        w_pt = float(size_mm[0]) * 72.0 / 25.4
        h_pt = float(size_mm[1]) * 72.0 / 25.4
    else:
        dpi_info = im.info.get("dpi")
        dpi = float(dpi_info[0]) if (dpi_info and dpi_info[0]) else 300.0
        w_pt = w_px * 72.0 / dpi
        h_pt = h_px * 72.0 / dpi

    arr = np.asarray(im)
    pdf = pikepdf.new()
    xobj = pikepdf.Stream(pdf, zlib.compress(arr.tobytes()))
    xobj.stream_dict = pikepdf.Dictionary(
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Image,
        Width=w_px,
        Height=h_px,
        ColorSpace=pikepdf.Name.DeviceRGB,
        BitsPerComponent=8,
        Filter=pikepdf.Name.FlateDecode,
    )
    resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xobj))
    content = f"q {w_pt:.4f} 0 0 {h_pt:.4f} 0 0 cm /Im0 Do Q"
    page_dict = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, w_pt, h_pt],
        TrimBox=[0, 0, w_pt, h_pt],
        Resources=resources,
        Contents=pdf.make_stream(content.encode()),
    )
    pdf.pages.append(pikepdf.Page(pdf.make_indirect(page_dict)))
    pdf.save(out_pdf)
    return {
        "out_path": str(out_pdf),
        "w_px": w_px,
        "h_px": h_px,
        "page_mm": (round(w_pt * 25.4 / 72.0, 1), round(h_pt * 25.4 / 72.0, 1)),
    }
