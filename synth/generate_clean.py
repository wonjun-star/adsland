"""인쇄 규격에 맞는 정상 PDF 생성기 — 결함은 생성 파라미터로만 주입한다.

설계 원칙: 기존 PDF를 mutate하지 않는다. 단일 generate(product, out_path, defects=[...])
함수가 결함 파라미터를 받아 처음부터 그 상태로 생성한다. 그래야 manifest(정답 라벨)가
'코드로' 보장되고, ground truth가 흔들리지 않는다.

정상 PDF가 결정론적으로 보장하는 것:
- MediaBox = 재단크기 + 사방 3mm bleed, TrimBox = 중앙 재단 영역, BleedBox = MediaBox.
  박스는 reportlab 생성 후 pikepdf 후처리로 확정한다 (reportlab 박스 API에 의존하지 않음).
- 배경/디자인 요소는 DeviceCMYK로만 칠하고 bleed 끝까지 채운다 (총잉크 300% 이하).
- 사진: PIL 그라디언트+노이즈 → CMYK JPEG → reportlab DCT 패스스루로 DeviceCMYK 임베딩.
  픽셀 수를 배치 크기에서 역산해 유효 해상도 300dpi 이상(기본 320dpi).
- 본문 텍스트는 K100(0,0,0,1), 임베딩 폰트(reportlab 동봉 Vera.ttf) 사용.
- 텍스트·중요 객체는 재단선에서 3mm 이상 안쪽 (실제 배치는 5mm 인셋).
- sticker/label: Separation 'CutContour' 별색 칼선(둥근 사각형, 1pt)을 재단선 위에 스트로크.
  생성 직후 pikepdf로 별색 존재/박스 좌표/페이지 수를 재검증한다 (불일치 시 예외).

재현성: Canvas(invariant=1) + pikepdf save(deterministic_id=True) + 사진 노이즈 시드 고정.
"""

from __future__ import annotations

import hashlib
import io
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pikepdf
import reportlab
from PIL import Image
from reportlab.lib.colors import CMYKColorSep
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from synth.manifest import DefectSpec, ManifestEntry, OrderSpec

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MM_PER_PT = 25.4 / 72.0
PT_PER_MM = 72.0 / 25.4

BLEED_MM = 3.0  # 정상 bleed (사방)
SAFETY_MM = 3.0  # 재단선 안전여백 기준
CONTENT_INSET_MM = 5.0  # 실제 콘텐츠 배치 인셋 (안전여백 3mm + 여유 2mm)
CLEAN_PHOTO_DPI = 320.0  # 유효 해상도 기준 300dpi + 여유
DEFAULT_SEED = 20260703

EMBED_FONT = "Vera"  # reportlab 동봉 TTF — 서브셋 임베딩됨(FontFile2)


def mm_to_pt(v: float) -> float:
    return v * PT_PER_MM


def pt_to_mm(v: float) -> float:
    return v * MM_PER_PT


@dataclass(frozen=True)
class ProductSpec:
    product: str
    size_mm: tuple[float, float]  # 재단 크기 (w, h)
    dieline: bool  # CutContour 별색 칼선 포함 여부


#: 상품 9종 기본 규격 (재단 크기 기준). 앞 5종은 eval 코퍼스 기준(CORE_PRODUCTS),
#: 뒤 3종은 데모 쇼케이스용 신규 낱장 인쇄물(엽서·떡메모지·포토카드, 칼선 없음),
#: banner 는 실사출력 대형(현수막) — 생성/렌더 부담을 줄이려 900x600mm 기준.
PRODUCTS: dict[str, ProductSpec] = {
    "sticker": ProductSpec("sticker", (90.0, 90.0), dieline=True),
    "namecard": ProductSpec("namecard", (90.0, 50.0), dieline=False),
    "flyer": ProductSpec("flyer", (148.0, 210.0), dieline=False),
    "poster": ProductSpec("poster", (420.0, 594.0), dieline=False),
    "label": ProductSpec("label", (60.0, 60.0), dieline=True),
    "postcard": ProductSpec("postcard", (100.0, 148.0), dieline=False),
    "memopad": ProductSpec("memopad", (100.0, 100.0), dieline=False),
    "photocard": ProductSpec("photocard", (55.0, 85.0), dieline=False),
    "banner": ProductSpec("banner", (900.0, 600.0), dieline=False),
}

#: eval 코퍼스(inject_defects) 가 쓰는 기존 5종. manifest.json/corpus 의 정답은 이 5종에
#: 고정되어 있으므로, PRODUCTS 에 신규 상품을 추가해도 코퍼스 구성은 흔들리면 안 된다.
#: 신규 3종은 synth/showcase.py 의 데모 세트에서만 쓴다.
CORE_PRODUCTS: tuple[str, ...] = ("sticker", "namecard", "flyer", "poster", "label")

# 변형(variant)별 배경/밴드 색 — 전부 DeviceCMYK, 잉크 합이 낮게 (배경 ≤30%, 밴드 ≤130%)
_BG_COLORS = [(0.02, 0.06, 0.18, 0.00), (0.14, 0.02, 0.05, 0.00)]
_BAND_COLORS = [(0.75, 0.25, 0.05, 0.02), (0.05, 0.55, 0.65, 0.00)]


def photo_size_mm(file_w_mm: float, file_h_mm: float) -> tuple[float, float]:
    """사진 배치 크기(mm). 큰 상품(포스터)은 생성 시간/용량을 위해 물리 크기를 캡.

    테스트가 유효 해상도(픽셀수 ÷ 배치 inch)를 역산할 때 같은 함수를 쓴다.
    """
    return (min(0.50 * file_w_mm, 110.0), min(0.35 * file_h_mm, 80.0))


def _stable_seed(*parts: Any) -> int:
    """프로세스/플랫폼 무관 안정 시드 (hash()는 PYTHONHASHSEED에 흔들리므로 sha256 사용)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _ensure_fonts() -> None:
    if EMBED_FONT not in pdfmetrics.getRegisteredFontNames():
        vera = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
        pdfmetrics.registerFont(TTFont(EMBED_FONT, str(vera)))


def _fit_font_size(text: str, font: str, max_size: float, avail_pt: float, min_size: float = 5.0) -> float:
    """폭에 맞게 폰트 크기를 줄인다 (작은 상품에서 텍스트가 안전영역을 넘지 않도록)."""
    size = max_size
    while size > min_size and pdfmetrics.stringWidth(text, font, size) > avail_pt:
        size -= 0.5
    return size


def _photo_reader(px_w: int, px_h: int, seed: int, mode: str) -> ImageReader:
    """그라디언트+노이즈 사진 이미지 → JPEG(BytesIO) → ImageReader.

    - mode="cmyk": PIL convert("CMYK") 후 JPEG 저장(Adobe 마커, 반전 저장) —
      reportlab이 DCT 패스스루로 4채널 DeviceCMYK + Decode[1 0 ...] 반전 처리를 넣는다.
    - mode="rgb": 3채널 JPEG → DeviceRGB 이미지 (colorspace 결함용).
    - 채널값을 [45, 235]로 클립 → PIL RGB→CMYK 변환(K=0)의 최악 잉크 합이
      (765-135)/2.55 ≈ 247% 로 300% 이하가 항상 보장된다.
    """
    rng = np.random.default_rng(seed)
    xs = np.linspace(0.0, 1.0, px_w, dtype=np.float64)
    ys = np.linspace(0.0, 1.0, px_h, dtype=np.float64)
    g = np.outer(ys, xs)  # (h, w) 대각 그라디언트
    r = 70.0 + 130.0 * g
    gr = 90.0 + 90.0 * (1.0 - g)
    b = 100.0 + 80.0 * np.abs(np.sin(3.0 * math.pi * g))
    arr = np.stack([r, gr, b], axis=-1)
    arr += rng.normal(0.0, 7.0, arr.shape)
    arr = np.clip(arr, 45.0, 235.0).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")
    if mode == "cmyk":
        img = img.convert("CMYK")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return ImageReader(buf)


def _resolve_defects(
    defects: Iterable[DefectSpec | Mapping[str, Any] | str] | None,
    spec: ProductSpec,
    order: OrderSpec,
) -> list[DefectSpec]:
    """결함 목록 정규화 + 파라미터 기본값 확정. 확정된 파라미터가 manifest에 그대로 기록된다."""
    resolved: list[DefectSpec] = []
    seen: set[str] = set()
    for raw in defects or []:
        if isinstance(raw, str):
            d = DefectSpec(id=raw)
        elif isinstance(raw, DefectSpec):
            d = raw.model_copy(deep=True)
        elif isinstance(raw, Mapping):
            d = DefectSpec.model_validate(dict(raw))
        else:
            raise TypeError(f"결함 지정 형식 오류: {raw!r}")
        if d.id in seen:
            raise ValueError(f"결함 중복 주입: {d.id}")
        seen.add(d.id)

        p = dict(d.params)
        if d.id == "dieline" and not spec.dieline:
            raise ValueError(f"dieline 결함은 칼선 상품(sticker/label) 전용: {spec.product}")
        if d.id == "resolution":
            p.setdefault("dpi", 96.0)  # 72~120 저해상도 범위
        elif d.id == "colorspace":
            p.setdefault("mode", "image")  # image=RGB JPEG, fill=DeviceRGB 채움(rg 연산자)
            if p["mode"] not in ("image", "fill"):
                raise ValueError(f"colorspace mode 오류: {p['mode']}")
        elif d.id == "font_embed":
            p.setdefault("font", "Helvetica")  # base-14 → FontDescriptor/FontFile 없음
        elif d.id == "trim_safety":
            p.setdefault("inset_mm", 1.0)  # 재단선에서 1mm 안쪽 = 3mm 안전여백 위반
        elif d.id == "ink_total":
            p.setdefault("cmyk", [0.9, 0.9, 0.9, 0.9])
            p["total_percent"] = round(sum(p["cmyk"]) * 100.0, 1)  # 파라미터 일관성 보장
        elif d.id == "black_type":
            p.setdefault("cmyk", [0.6, 0.5, 0.4, 1.0])  # 4도 혼합 검정 (합 250%)
        elif d.id == "page_size":
            w, h = order.size_mm
            p.setdefault("file_size_mm", [w - 5.0, h - 5.0])  # 주문과 5mm 불일치
        elif d.id == "page_count":
            p.setdefault("file_page_count", order.page_count + 1)
        elif d.id == "transparency":
            p.setdefault("alpha", 0.5)  # ExtGState /ca 0.5
        elif d.id == "min_line":
            p.setdefault("width_pt", 0.05)  # 0.25pt 기준 미달 초극세선
        resolved.append(DefectSpec(id=d.id, params=p))
    return resolved


def _draw_page(
    c: Canvas,
    *,
    spec: ProductSpec,
    fw_mm: float,
    fh_mm: float,
    bleed_mm: float,
    ids: dict[str, DefectSpec],
    variant: int,
    reader: ImageReader,
    pw_mm: float,
    ph_mm: float,
    body_font: str,
    body_cmyk: tuple[float, float, float, float],
    page_no: int,
    total_pages: int,
) -> None:
    """페이지 1장 드로잉. 좌표계: pt, 원점 = MediaBox 좌하단, 재단 영역은 (b,b)부터."""
    b = mm_to_pt(bleed_mm)
    tw, th = mm_to_pt(fw_mm), mm_to_pt(fh_mm)
    mw, mh = tw + 2 * b, th + 2 * b
    ox, oy = b, b  # 재단 영역 원점
    inset = mm_to_pt(CONTENT_INSET_MM)

    bg = _BG_COLORS[variant % len(_BG_COLORS)]
    band = _BAND_COLORS[variant % len(_BAND_COLORS)]

    # 1) 배경 — DeviceCMYK, bleed 끝까지
    c.setFillColorCMYK(*bg)
    c.rect(0, 0, mw, mh, stroke=0, fill=1)
    # 2) 하단 밴드 — bleed 끝까지 (배경/밴드는 의도적으로 재단선을 넘는 디자인 요소)
    c.setFillColorCMYK(*band)
    c.rect(0, 0, mw, 0.18 * mh, stroke=0, fill=1)

    # 3) [ink_total] 잉크 과다 영역 — 안전영역 안쪽 큰 사각형, 벡터 k 연산자로 기록됨
    if "ink_total" in ids:
        ci, mi, yi, ki = ids["ink_total"].params["cmyk"]
        c.setFillColorCMYK(ci, mi, yi, ki)
        c.rect(ox + 0.15 * tw, oy + 0.12 * th, 0.70 * tw, 0.25 * th, stroke=0, fill=1)

    # 4) 타이틀 — 항상 K100 + 임베딩 폰트 (font_embed/black_type 결함은 '본문'에만 적용)
    title = f"{spec.product.upper()} SAMPLE V{variant + 1}"
    tsize = _fit_font_size(title, EMBED_FONT, 14.0, tw - 2 * inset)
    c.setFillColorCMYK(0, 0, 0, 1)
    c.setFont(EMBED_FONT, tsize)
    y_title = oy + th - inset - tsize
    c.drawString(ox + inset, y_title, title)

    # 5) 사진 — 가로 중앙, 타이틀 아래 3mm
    pw, ph = mm_to_pt(pw_mm), mm_to_pt(ph_mm)
    px = ox + (tw - pw) / 2.0
    py = y_title - mm_to_pt(3.0) - ph
    c.drawImage(reader, px, py, width=pw, height=ph, mask=None)

    # 6) [transparency] 반투명 오버레이 — ExtGState /ca 0.5 생성
    if "transparency" in ids:
        c.setFillAlpha(float(ids["transparency"].params["alpha"]))
        c.setFillColorCMYK(0.0, 0.65, 0.20, 0.0)
        c.rect(ox + 0.30 * tw, py - 0.02 * th, 0.40 * tw, 0.12 * th, stroke=0, fill=1)
        c.setFillAlpha(1.0)

    # 7) [colorspace mode=fill] DeviceRGB 채움 — 콘텐츠 스트림 rg 연산자
    if "colorspace" in ids and ids["colorspace"].params["mode"] == "fill":
        c.setFillColorRGB(0.15, 0.45, 0.85)
        c.rect(ox + 0.62 * tw, oy + 0.15 * th, 0.22 * tw, 0.08 * th, stroke=0, fill=1)

    # 8) 본문 텍스트 — 기본 K100 + Vera / 결함 시 혼합검정·Helvetica 로 대체
    lines = [
        "Print intake synthetic sample",
        f"Trim {fw_mm:g} x {fh_mm:g} mm / bleed {bleed_mm:g} mm",
    ]
    if total_pages > 1:
        lines.append(f"Page {page_no + 1} of {total_pages}")
    bsize = min(_fit_font_size(ln, body_font, 8.0, tw - 2 * inset) for ln in lines)
    c.setFillColorCMYK(*body_cmyk)
    c.setFont(body_font, bsize)
    y_body = py - mm_to_pt(4.0)
    for i, ln in enumerate(lines):
        c.drawString(ox + inset, y_body - i * 1.5 * bsize, ln)

    # 9) [min_line] 초극세선 — K100 스트로크, 콘텐츠 스트림 'w' 연산자로 기록됨
    if "min_line" in ids:
        c.setStrokeColorCMYK(0, 0, 0, 1)
        c.setLineWidth(float(ids["min_line"].params["width_pt"]))
        c.line(ox + 0.15 * tw, oy + 0.10 * th, ox + 0.85 * tw, oy + 0.10 * th)

    # 10) [trim_safety] 재단선 안전여백 위반 텍스트 — 좌하단 모서리 inset_mm 지점
    if "trim_safety" in ids:
        vio = mm_to_pt(float(ids["trim_safety"].params["inset_mm"]))
        c.setFillColorCMYK(0, 0, 0, 1)
        c.setFont(EMBED_FONT, 7.0)
        c.drawString(ox + vio, oy + vio, "CUT EDGE TEXT")

    # 11) 칼선 — Separation 'CutContour' 별색 스트로크, 재단선 위 둥근 사각형 1pt
    if spec.dieline and "dieline" not in ids:
        sep = CMYKColorSep(0, 1, 0, 0, spotName="CutContour")
        c.setStrokeColor(sep)
        c.setLineWidth(1.0)
        c.roundRect(ox, oy, tw, th, min(tw, th) * 0.06, stroke=1, fill=0)


def _set_boxes(
    path: Path,
    media: tuple[float, float, float, float],
    trim: tuple[float, float, float, float],
) -> None:
    """pikepdf 후처리로 박스를 확정. reportlab의 박스 출력에 의존하지 않는다."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            page.obj["/MediaBox"] = pikepdf.Array(list(media))
            page.obj["/CropBox"] = pikepdf.Array(list(media))
            page.obj["/BleedBox"] = pikepdf.Array(list(media))
            page.obj["/TrimBox"] = pikepdf.Array(list(trim))
        pdf.save(path, deterministic_id=True)


def has_cutcontour(pdf: pikepdf.Pdf) -> bool:
    """페이지 리소스에서 [/Separation /CutContour ...] 색공간 존재 여부."""
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        if res is None:
            continue
        csd = res.get("/ColorSpace")
        if csd is None:
            continue
        for _name, cs in csd.items():
            try:
                if len(cs) >= 2 and str(cs[0]) == "/Separation" and str(cs[1]) == "/CutContour":
                    return True
            except Exception:
                continue
    return False


def _verify_output(
    path: Path,
    media: tuple[float, float, float, float],
    trim: tuple[float, float, float, float],
    pages: int,
    dieline_expected: bool,
) -> None:
    """생성 직후 자체 검증 — ground truth가 물리적으로 성립하는지 pikepdf로 재확인."""
    with pikepdf.open(path) as pdf:
        if len(pdf.pages) != pages:
            raise RuntimeError(f"{path.name}: 페이지 수 불일치 (기대 {pages}, 실제 {len(pdf.pages)})")
        for pno, page in enumerate(pdf.pages):
            for key, want in [("/MediaBox", media), ("/TrimBox", trim), ("/BleedBox", media)]:
                got = [float(v) for v in page.obj[key]]
                if any(abs(a - b) > 0.01 for a, b in zip(got, want)):
                    raise RuntimeError(f"{path.name} p{pno}: {key} 불일치 (기대 {want}, 실제 {got})")
        if has_cutcontour(pdf) != dieline_expected:
            raise RuntimeError(
                f"{path.name}: CutContour 별색 {'누락' if dieline_expected else '잔존'} (기대 {dieline_expected})"
            )


def generate(
    product: str,
    out_path: str | Path,
    defects: Iterable[DefectSpec | Mapping[str, Any] | str] | None = None,
    order: OrderSpec | None = None,
    variant: int = 0,
    seed: int = DEFAULT_SEED,
) -> ManifestEntry:
    """상품 PDF 생성 (defects 미지정 시 정상). 반환값 = manifest 엔트리(정답 라벨).

    - order: 주문 사양. page_size/page_count 결함은 order와 파일의 '불일치'로 구현되므로
      order가 정답 기준값이다 (기본: 상품 기본 규격 1페이지).
    - variant: 디자인 변형 (색/노이즈 시드만 바뀜, 규격 GT는 동일).
    """
    if product not in PRODUCTS:
        raise KeyError(f"알 수 없는 상품: {product} (허용: {sorted(PRODUCTS)})")
    spec = PRODUCTS[product]
    order = order if order is not None else OrderSpec(size_mm=spec.size_mm, page_count=1)
    resolved = _resolve_defects(defects, spec, order)
    ids = {d.id: d for d in resolved}

    # 파일 측 실측값 (주문과 다르게 만드는 결함 반영)
    fw_mm, fh_mm = order.size_mm
    if "page_size" in ids:
        fw_mm, fh_mm = (float(v) for v in ids["page_size"].params["file_size_mm"])
    pages = order.page_count
    if "page_count" in ids:
        pages = int(ids["page_count"].params["file_page_count"])
    bleed_mm = 0.0 if "bleed" in ids else BLEED_MM

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_fonts()

    # 사진: 배치 크기(mm) → 목표 dpi 역산으로 픽셀 수 결정 (ceil → 유효 dpi ≥ 목표)
    pw_mm, ph_mm = photo_size_mm(fw_mm, fh_mm)
    photo_mode = "cmyk"
    photo_dpi = CLEAN_PHOTO_DPI
    if "colorspace" in ids and ids["colorspace"].params["mode"] == "image":
        photo_mode = "rgb"
    if "resolution" in ids:
        photo_dpi = float(ids["resolution"].params["dpi"])
    px_w = max(8, math.ceil(pw_mm / 25.4 * photo_dpi))
    px_h = max(8, math.ceil(ph_mm / 25.4 * photo_dpi))
    reader = _photo_reader(px_w, px_h, _stable_seed(seed, product, variant, photo_mode, px_w, px_h), photo_mode)

    body_font = EMBED_FONT
    if "font_embed" in ids:
        body_font = str(ids["font_embed"].params["font"])
    body_cmyk = (0.0, 0.0, 0.0, 1.0)
    if "black_type" in ids:
        body_cmyk = tuple(float(v) for v in ids["black_type"].params["cmyk"])

    b = mm_to_pt(bleed_mm)
    tw, th = mm_to_pt(fw_mm), mm_to_pt(fh_mm)
    mw, mh = tw + 2 * b, th + 2 * b

    # invariant=1 → 생성일자/문서ID 고정 (재현 가능한 바이트 출력)
    # initialFontName: reportlab이 페이지 프리앰블에 기본 폰트(Helvetica) Tf를 넣어
    # 미임베딩 폰트가 모든 페이지 리소스에 끼는 것을 방지 — 정상 파일의 GT 보호
    c = Canvas(
        str(out_path),
        pagesize=(mw, mh),
        pageCompression=1,
        invariant=1,
        initialFontName=EMBED_FONT,
    )
    for pno in range(pages):
        _draw_page(
            c,
            spec=spec,
            fw_mm=fw_mm,
            fh_mm=fh_mm,
            bleed_mm=bleed_mm,
            ids=ids,
            variant=variant,
            reader=reader,
            pw_mm=pw_mm,
            ph_mm=ph_mm,
            body_font=body_font,
            body_cmyk=body_cmyk,
            page_no=pno,
            total_pages=pages,
        )
        c.showPage()
    c.save()

    media = (0.0, 0.0, round(mw, 3), round(mh, 3))
    trim = (round(b, 3), round(b, 3), round(b + tw, 3), round(b + th, 3))
    _set_boxes(out_path, media, trim)
    _verify_output(out_path, media, trim, pages, spec.dieline and "dieline" not in ids)

    return ManifestEntry(file=out_path.as_posix(), product=product, order=order, defects=resolved)


def main() -> None:
    """CLI: 기존 상품 5종 정상 PDF → data/samples/clean/ (신규 3종은 showcase 세트에서 다룸)"""
    out_dir = PROJECT_ROOT / "data" / "samples" / "clean"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in CORE_PRODUCTS:
        path = out_dir / f"clean_{name}.pdf"
        generate(name, path)
        print(f"[synth] 정상 샘플 생성: {path}")


if __name__ == "__main__":
    main()
