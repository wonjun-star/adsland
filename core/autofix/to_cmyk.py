"""autofix: RGB → CMYK 자동 변환.

애즈랜드 가이드는 CMYK 필수인데 RGB로 만든 파일이 흔하다. 이 보정은 페이지를 300dpi로
래스터화한 뒤 RGB를 CMYK로 변환(GCR: 회색성분을 K로 이관해 검정에 먹을 넣고 총잉크량도 낮춤)해
DeviceCMYK 이미지로 재조립한다. 원본 페이지 크기(Media/Trim/Bleed)는 그대로 보존한다.

한계 (정직):
- ICC 프로파일(Japan Color 2001 Coated 등) 없이 하는 근사 변환이라 화면 색과 미세하게 다를 수
  있다 — 그래서 고객에게 '색이 약간 달라질 수 있음'을 반드시 고지한다.
- 래스터화되므로 텍스트·벡터의 선명함이 300dpi 픽셀로 바뀐다(칼선 별색은 이 보정 대상 밖).

원본은 절대 덮어쓰지 않는다 (reversible).
"""

from __future__ import annotations

import zlib
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium
from PIL import Image

DPI = 300
_SCALE = DPI / 72.0


def _rgb_to_cmyk_gcr(rgb: np.ndarray) -> np.ndarray:
    """RGB(H,W,3, uint8) → CMYK(H,W,4, uint8). GCR로 K 생성(검정에 먹, 총잉크량↓).

    표준 GCR: c,m,y = 1-r,1-g,1-b; k=min(c,m,y); 나머지를 K를 뺀 값으로 정규화.
    순수 검정(0,0,0) → K=1(먹1도), 총잉크 100%. 순수 색은 K=0 유지.
    """
    a = rgb.astype(np.float32) / 255.0
    c = 1.0 - a[..., 0]
    m = 1.0 - a[..., 1]
    y = 1.0 - a[..., 2]
    k = np.minimum(np.minimum(c, m), y)
    denom = np.clip(1.0 - k, 1e-6, None)
    c = (c - k) / denom
    m = (m - k) / denom
    y = (y - k) / denom
    cmyk = np.stack([c, m, y, k], axis=-1)
    cmyk = np.clip(cmyk, 0.0, 1.0)
    return (cmyk * 255.0 + 0.5).astype(np.uint8)


def to_cmyk(
    pdf_path: str | Path,
    out_path: str | Path,
    preview_dir: str | Path | None = None,
) -> dict:
    """모든 페이지를 CMYK로 변환한 새 PDF 생성. 페이지 크기(박스)는 보존.

    반환: {out_path, previews, pages}
    """
    pdf_path, out_path = Path(pdf_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 원본 박스 읽기 (크기 보존)
    with pikepdf.open(pdf_path) as src:
        boxes = []
        for page in src.pages:
            media = [float(v) for v in page["/MediaBox"]]
            trim = [float(v) for v in page["/TrimBox"]] if "/TrimBox" in page else list(media)
            bleed = [float(v) for v in page["/BleedBox"]] if "/BleedBox" in page else list(media)
            boxes.append((media, trim, bleed))

    doc = pdfium.PdfDocument(str(pdf_path))
    out = pikepdf.new()
    try:
        for i, (media, trim, bleed) in enumerate(boxes):
            rgb = doc[i].render(scale=_SCALE).to_pil().convert("RGB")
            cmyk = _rgb_to_cmyk_gcr(np.asarray(rgb))
            h, w = cmyk.shape[:2]

            xobj = pikepdf.Stream(out, zlib.compress(cmyk.tobytes()))
            xobj.stream_dict = pikepdf.Dictionary(
                Type=pikepdf.Name.XObject,
                Subtype=pikepdf.Name.Image,
                Width=w,
                Height=h,
                ColorSpace=pikepdf.Name.DeviceCMYK,
                BitsPerComponent=8,
                Filter=pikepdf.Name.FlateDecode,
            )
            resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xobj))

            mx0, my0, mx1, my1 = media
            mw, mh = mx1 - mx0, my1 - my0
            # 이미지를 MediaBox에 꽉 채운다 (원점 이동은 박스 좌표로 처리)
            content = f"q {mw:.4f} 0 0 {mh:.4f} {mx0:.4f} {my0:.4f} cm /Im0 Do Q"

            page_dict = pikepdf.Dictionary(
                Type=pikepdf.Name.Page,
                MediaBox=media,
                TrimBox=trim,
                BleedBox=bleed,
                Resources=resources,
                Contents=out.make_stream(content.encode()),
            )
            out.pages.append(pikepdf.Page(out.make_indirect(page_dict)))
        out.save(out_path)
    finally:
        doc.close()

    previews: list[dict] = []
    if preview_dir is not None:
        previews = _make_previews(pdf_path, out_path, Path(preview_dir))

    return {"out_path": str(out_path), "previews": previews, "pages": len(boxes)}


def _make_previews(before_pdf: Path, after_pdf: Path, pv_dir: Path, max_w: int = 640) -> list[dict]:
    pv_dir.mkdir(parents=True, exist_ok=True)
    previews: list[dict] = []
    docs = (pdfium.PdfDocument(str(before_pdf)), pdfium.PdfDocument(str(after_pdf)))
    try:
        n = min(len(docs[0]), len(docs[1]))
        for i in range(n):
            pair = {}
            for tag, doc in zip(("before", "after"), docs):
                img = doc[i].render(scale=1.5).to_pil().convert("RGB")
                if img.width > max_w:
                    img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
                p = pv_dir / f"{before_pdf.stem}_cmyk_p{i}_{tag}.png"
                img.save(p)
                pair[tag] = str(p)
            previews.append(pair)
    finally:
        for d in docs:
            d.close()
    return previews
