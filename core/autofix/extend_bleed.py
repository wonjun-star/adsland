"""autofix: 재단여백(bleed) 자동 연장 — 프로토타입 유일의 autofix.

방식:
1) 원본에서 칼선(별색 CutContour류 스트로크)을 벡터로 캡처하고, 칼선을 뺀 사본을 만든다.
2) 사본을 300dpi로 래스터화 → 가장자리 픽셀 연장(edge replicate) → CMYK 이미지로 재합성.
3) 캡처한 칼선을 새 좌표계로 옮겨 벡터 그대로 다시 얹는다 (별색·선폭 보존).

원본은 절대 덮어쓰지 않는다 (reversible). 페이지 기하는 픽셀 수에서 역산해
유효 해상도가 정확히 300dpi가 되게 한다 (반올림 오차로 299.9dpi warn이 뜨던 문제 방지).

한계 (본개발 과제, ADR-002): 칼선 외 벡터/텍스트는 이미지가 된다.
색 변환은 PIL 나이브 CMYK(잉크 총량 ≤300% 보장)이며 ICC 기반 변환은 본개발에서.
"""

from __future__ import annotations

import math
import re
import zlib
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium
from PIL import Image

from core.preflight.contentstream import Matrix, mat_mul
from core.preflight.engine import PT_PER_MM

DPI = 300
_SCALE = DPI / 72.0  # pt → px

#: 칼선으로 취급하는 별색 이름 패턴
_CUT_NAME_RE = re.compile(r"cut|die|thru.?cut|kiss", re.IGNORECASE)

_PATH_OPS = {"m", "l", "c", "v", "y", "re", "h"}
_PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}


# ---------------------------------------------------------------- 칼선 캡처/제거


def _resolve_stroke_space(operand, resources) -> tuple[str, str]:
    """CS 연산자 피연산자 → (분류, 별색이름). 분류 ∈ {Separation, other}."""
    name = str(operand)
    if name in ("/DeviceCMYK", "/DeviceRGB", "/DeviceGray", "/Pattern"):
        return ("other", "")
    try:
        cs_dict = resources["/ColorSpace"]
        cs_obj = cs_dict[name]
        if isinstance(cs_obj, pikepdf.Array) and len(cs_obj) >= 2 and str(cs_obj[0]) == "/Separation":
            return ("Separation", str(cs_obj[1]).lstrip("/"))
    except Exception:
        pass
    return ("other", "")


def _capture_and_strip_dielines(
    src: Path, stripped_out: Path
) -> tuple[list[list[dict]], bool]:
    """페이지별 칼선 스트로크 캡처 + 칼선 제거 사본 저장.

    반환: (per_page_captures, any_found). 캡처 항목:
    {"ctm": Matrix, "ops": [(floats, op)], "width": float, "spot": str}
    """
    per_page: list[list[dict]] = []
    found = False
    with pikepdf.open(src) as pdf:
        for page in pdf.pages:
            resources = page.get("/Resources", pikepdf.Dictionary())
            try:
                instructions = pikepdf.parse_content_stream(page)
            except Exception:
                per_page.append([])
                continue

            captures: list[dict] = []
            kept: list = []
            path_orig: list = []          # 아직 칠해지지 않은 경로 세그먼트 (원본 그대로)
            path_raw: list[tuple[list[float], str]] = []
            ctm: Matrix = (1, 0, 0, 1, 0, 0)
            stack: list[tuple] = []
            stroke_kind, spot = "other", ""
            width = 1.0

            for operands, operator in instructions:
                op = str(operator)
                if op in _PATH_OPS:
                    path_orig.append((operands, operator))
                    path_raw.append(([float(v) for v in operands], op))
                    continue
                if op in ("W", "W*"):  # 클리핑은 경로와 함께 유지 (칼선에는 안 쓰임)
                    path_orig.append((operands, operator))
                    path_raw.append(([], op))
                    continue
                if op in _PAINT_OPS:
                    is_dieline = (
                        op in ("S", "s")
                        and stroke_kind == "Separation"
                        and bool(_CUT_NAME_RE.search(spot))
                    )
                    if is_dieline:
                        captures.append(
                            {"ctm": ctm, "ops": list(path_raw), "width": width, "spot": spot}
                        )
                        found = True
                    else:
                        kept.extend(path_orig)
                        kept.append((operands, operator))
                    path_orig, path_raw = [], []
                    continue

                # 상태 연산자 — 추적 후 그대로 유지
                if op == "q":
                    stack.append((ctm, stroke_kind, spot, width))
                elif op == "Q":
                    if stack:
                        ctm, stroke_kind, spot, width = stack.pop()
                elif op == "cm" and len(operands) == 6:
                    m = tuple(float(v) for v in operands)
                    ctm = mat_mul(m, ctm)  # type: ignore[arg-type]
                elif op == "w" and operands:
                    width = float(operands[0])
                elif op == "CS" and operands:
                    stroke_kind, spot = _resolve_stroke_space(operands[0], resources)
                elif op in ("K", "G", "RG"):
                    stroke_kind, spot = "other", ""
                kept.append((operands, operator))

            kept.extend(path_orig)  # 칠해지지 않고 남은 경로 (방어적)
            if captures:
                page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(kept))
            per_page.append(captures)
        pdf.save(stripped_out)
    return per_page, found


def _dieline_fragment(captures: list[dict], dx: float, dy: float) -> str:
    """캡처한 칼선을 새 페이지 좌표로 재생하는 콘텐츠 조각. 색공간 키 /CSCut 가정."""
    parts = [f"q 1 0 0 1 {dx:.4f} {dy:.4f} cm"]
    for cap in captures:
        a, b, c, d, e, f = cap["ctm"]
        parts.append(f"q {a:.6f} {b:.6f} {c:.6f} {d:.6f} {e:.4f} {f:.4f} cm")
        parts.append(f"/CSCut CS 1 SCN {cap['width']:.4f} w")
        for nums, op in cap["ops"]:
            if op in ("W", "W*"):
                continue
            parts.append((" ".join(f"{v:.4f}" for v in nums) + f" {op}").strip())
        parts.append("S Q")
    parts.append("Q")
    return "\n".join(parts)


# ---------------------------------------------------------------- 본체


def extend_bleed(
    pdf_path: str | Path,
    out_path: str | Path,
    bleed_mm: float = 3.0,
    preview_dir: str | Path | None = None,
) -> dict:
    """모든 페이지의 bleed를 bleed_mm로 연장한 새 PDF 생성.

    반환: {out_path, previews: [{before, after}], bleed_mm, dieline_preserved}
    """
    pdf_path, out_path = Path(pdf_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bleed_px = math.ceil(bleed_mm * PT_PER_MM * _SCALE)

    # 1) 칼선 캡처 + 칼선 제거 사본 (래스터에 칼선이 구워지지 않게)
    stripped = out_path.parent / f"{out_path.stem}_nodl.tmp.pdf"
    captures_per_page, has_dieline = _capture_and_strip_dielines(pdf_path, stripped)
    render_src = stripped if has_dieline else pdf_path

    # 페이지 박스는 원본에서 읽는다 (사본과 동일하지만 의미상 원본이 기준)
    with pikepdf.open(pdf_path) as pdf:
        boxes = []
        for page in pdf.pages:
            media = [float(v) for v in page["/MediaBox"]]
            trim = [float(v) for v in page["/TrimBox"]] if "/TrimBox" in page else list(media)
            boxes.append((media, trim))

    doc = pdfium.PdfDocument(str(render_src))
    records: list[dict] = []
    try:
        for i, (media, trim) in enumerate(boxes):
            img = doc[i].render(scale=_SCALE).to_pil().convert("RGB")
            mx0, my0, mx1, my1 = media
            tx0, ty0, tx1, ty1 = trim
            # 재단 영역 → 픽셀 크롭 (렌더 원점 = MediaBox 좌상단)
            left = int(round((tx0 - mx0) * _SCALE))
            top = int(round((my1 - ty1) * _SCALE))
            right = int(round((tx1 - mx0) * _SCALE))
            bottom = int(round((my1 - ty0) * _SCALE))
            left, top = max(0, left), max(0, top)
            right, bottom = min(img.width, right), min(img.height, bottom)
            trim_img = img.crop((left, top, right, bottom))

            arr = np.asarray(trim_img)
            extended = np.pad(arr, ((bleed_px, bleed_px), (bleed_px, bleed_px), (0, 0)), mode="edge")
            ext_img = Image.fromarray(extended, "RGB")

            # 원본 사용자 좌표 → 새 페이지 좌표 오프셋 (칼선 재생용, 크롭 반올림 반영)
            x0c = mx0 + left / _SCALE
            y0c = my1 - bottom / _SCALE
            bleed_pt_px = bleed_px / _SCALE
            records.append(
                {
                    "cmyk": ext_img.convert("CMYK"),
                    "captures": captures_per_page[i] if i < len(captures_per_page) else [],
                    "dx": bleed_pt_px - x0c,
                    "dy": bleed_pt_px - y0c,
                }
            )
    finally:
        doc.close()

    _write_cmyk_pdf(records, bleed_px, out_path)
    stripped.unlink(missing_ok=True)

    # 미리보기: 전 = 원본 렌더, 후 = 산출물 렌더 (칼선 포함 실물 그대로)
    previews: list[dict] = []
    if preview_dir is not None:
        pv = Path(preview_dir)
        pv.mkdir(parents=True, exist_ok=True)
        previews = _make_previews(pdf_path, out_path, pv)

    return {
        "out_path": str(out_path),
        "previews": previews,
        "bleed_mm": round(bleed_px / _SCALE / PT_PER_MM, 3),
        "dieline_preserved": has_dieline,
    }


def _make_previews(before_pdf: Path, after_pdf: Path, pv_dir: Path, max_w: int = 640) -> list[dict]:
    previews = []
    docs = (pdfium.PdfDocument(str(before_pdf)), pdfium.PdfDocument(str(after_pdf)))
    try:
        n = min(len(docs[0]), len(docs[1]))
        for i in range(n):
            pair = {}
            for tag, doc in zip(("before", "after"), docs):
                img = doc[i].render(scale=1.5).to_pil().convert("RGB")
                if img.width > max_w:
                    img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
                p = pv_dir / f"{before_pdf.stem}_p{i}_{tag}.png"
                img.save(p)
                pair[tag] = str(p)
            previews.append(pair)
    finally:
        for d in docs:
            d.close()
    return previews


def _write_cmyk_pdf(records: list[dict], bleed_px: int, out_path: Path) -> None:
    """CMYK 래스터 + (있으면) 벡터 칼선으로 PDF 재조립.

    페이지 기하는 픽셀 수에서 역산 → 유효 해상도 정확히 300dpi.
    MediaBox=BleedBox=trim+bleed, TrimBox=재단영역.
    """
    pdf = pikepdf.new()
    for rec in records:
        img: Image.Image = rec["cmyk"]
        mw = img.width * 72.0 / DPI
        mh = img.height * 72.0 / DPI
        inset = bleed_px * 72.0 / DPI

        xobj = pikepdf.Stream(pdf, zlib.compress(img.tobytes()))
        xobj.stream_dict = pikepdf.Dictionary(
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=img.width,
            Height=img.height,
            ColorSpace=pikepdf.Name.DeviceCMYK,
            BitsPerComponent=8,
            Filter=pikepdf.Name.FlateDecode,
        )
        resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xobj))

        content = f"q {mw:.4f} 0 0 {mh:.4f} 0 0 cm /Im0 Do Q"
        captures = rec["captures"]
        if captures:
            spot = captures[0]["spot"] or "CutContour"
            tint_fn = pdf.make_indirect(
                pikepdf.Dictionary(
                    FunctionType=2,
                    Domain=[0, 1],
                    C0=[0, 0, 0, 0],
                    C1=[0, 1, 0, 0],  # 표시용 마젠타 (칼선 관례)
                    N=1,
                )
            )
            sep = pikepdf.Array(
                [pikepdf.Name.Separation, pikepdf.Name(f"/{spot}"), pikepdf.Name.DeviceCMYK, tint_fn]
            )
            resources.ColorSpace = pikepdf.Dictionary(CSCut=sep)
            content += "\n" + _dieline_fragment(captures, rec["dx"], rec["dy"])

        page_dict = pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[0, 0, mw, mh],
            TrimBox=[inset, inset, mw - inset, mh - inset],
            BleedBox=[0, 0, mw, mh],
            Resources=resources,
            Contents=pdf.make_stream(content.encode()),
        )
        pdf.pages.append(pikepdf.Page(pdf.make_indirect(page_dict)))
    pdf.save(out_path)
