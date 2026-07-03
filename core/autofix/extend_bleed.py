"""autofix: 재단여백(bleed) 자동 연장 — 프로토타입 유일의 autofix.

방식: 페이지를 300dpi로 래스터화 → 가장자리 픽셀 연장(edge replicate) →
CMYK 이미지로 재합성한 새 PDF 생성. 원본은 절대 덮어쓰지 않는다 (reversible).

한계 (본개발 과제, ADR 기록): 래스터화로 벡터/텍스트가 이미지가 된다.
색 변환은 PIL 나이브 CMYK(잉크 총량 ≤300% 보장)이며 ICC 기반 변환은 본개발에서.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium
from PIL import Image

from core.preflight.engine import PT_PER_MM

DPI = 300
_SCALE = DPI / 72.0  # pt → px


def _render_page_rgb(pdf_path: Path, page_index: int) -> tuple[Image.Image, tuple[float, float, float, float]]:
    """페이지 렌더 + TrimBox(pt, 페이지 좌표) 반환. TrimBox 없으면 MediaBox."""
    with pikepdf.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        media = [float(v) for v in page["/MediaBox"]]
        box = [float(v) for v in page["/TrimBox"]] if "/TrimBox" in page else list(media)
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        img = doc[page_index].render(scale=_SCALE).to_pil().convert("RGB")
    finally:
        doc.close()
    # 렌더 원점은 MediaBox 좌상단. TrimBox를 픽셀 좌표(top-left 기준)로 변환
    mx0, my0, _, my1 = media[0], media[1], media[2], media[3]
    x0, y0, x1, y1 = box
    left = (x0 - mx0) * _SCALE
    top = (my1 - y1) * _SCALE
    right = (x1 - mx0) * _SCALE
    bottom = (my1 - y0) * _SCALE
    return img, (left, top, right, bottom)


def extend_bleed(
    pdf_path: str | Path,
    out_path: str | Path,
    bleed_mm: float = 3.0,
    preview_dir: str | Path | None = None,
) -> dict:
    """모든 페이지의 bleed를 bleed_mm로 연장한 새 PDF 생성.

    반환: {out_path, previews: [{before, after}], bleed_mm}
    """
    pdf_path, out_path = Path(pdf_path), Path(out_path)
    bleed_px = int(round(bleed_mm * PT_PER_MM * _SCALE))

    with pikepdf.open(pdf_path) as src:
        n_pages = len(src.pages)

    pages_cmyk: list[Image.Image] = []
    previews: list[dict] = []
    trim_sizes_pt: list[tuple[float, float]] = []

    for i in range(n_pages):
        img, (left, top, right, bottom) = _render_page_rgb(pdf_path, i)
        trim = img.crop((int(round(left)), int(round(top)), int(round(right)), int(round(bottom))))
        trim_sizes_pt.append(((right - left) / _SCALE, (bottom - top) / _SCALE))

        arr = np.asarray(trim)
        extended = np.pad(arr, ((bleed_px, bleed_px), (bleed_px, bleed_px), (0, 0)), mode="edge")
        ext_img = Image.fromarray(extended, "RGB")
        pages_cmyk.append(ext_img.convert("CMYK"))

        if preview_dir is not None:
            pv = Path(preview_dir)
            pv.mkdir(parents=True, exist_ok=True)
            before_p = pv / f"{pdf_path.stem}_p{i}_before.png"
            after_p = pv / f"{pdf_path.stem}_p{i}_after.png"
            _preview(img.crop((int(left), int(top), int(right), int(bottom))), None, 0).save(before_p)
            _preview(ext_img.convert("RGB"), bleed_px, bleed_px).save(after_p)
            previews.append({"before": str(before_p), "after": str(after_p)})

    _write_cmyk_pdf(pages_cmyk, trim_sizes_pt, bleed_mm, out_path)
    return {"out_path": str(out_path), "previews": previews, "bleed_mm": bleed_mm}


def _preview(img: Image.Image, bleed_px: int | None, inset: int, max_w: int = 640) -> Image.Image:
    """미리보기 축소본. bleed_px가 있으면 재단선 위치에 가이드 표시용 여백 유지."""
    out = img.copy()
    if out.width > max_w:
        out = out.resize((max_w, int(out.height * max_w / out.width)), Image.LANCZOS)
    return out


def _write_cmyk_pdf(
    pages: list[Image.Image],
    trim_sizes_pt: list[tuple[float, float]],
    bleed_mm: float,
    out_path: Path,
) -> None:
    """CMYK 래스터 페이지들로 PDF 재조립. MediaBox=BleedBox=trim+bleed, TrimBox=재단영역."""
    bleed_pt = bleed_mm * PT_PER_MM
    pdf = pikepdf.new()
    for img, (tw, th) in zip(pages, trim_sizes_pt):
        mw, mh = tw + 2 * bleed_pt, th + 2 * bleed_pt
        raw = img.tobytes()  # CMYK 8bit
        xobj = pikepdf.Stream(pdf, zlib.compress(raw))
        xobj.stream_dict = pikepdf.Dictionary(
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=img.width,
            Height=img.height,
            ColorSpace=pikepdf.Name.DeviceCMYK,
            BitsPerComponent=8,
            Filter=pikepdf.Name.FlateDecode,
        )
        content = f"q {mw:.2f} 0 0 {mh:.2f} 0 0 cm /Im0 Do Q".encode()
        page_dict = pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[0, 0, mw, mh],
            TrimBox=[bleed_pt, bleed_pt, bleed_pt + tw, bleed_pt + th],
            BleedBox=[0, 0, mw, mh],
            Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xobj)),
            Contents=pdf.make_stream(content),
        )
        pdf.pages.append(pikepdf.Page(pdf.make_indirect(page_dict)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(out_path)
