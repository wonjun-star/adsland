"""colorspace 체크 테스트.

- 코퍼스 실파일 스팟: 결함 파일(image/fill 2모드) 검출 + 정상·타결함 파일 침묵
- 경계 fixture: ICC-RGB 이미지, Indexed(RGB) 이미지, RGB 스트로크, 비표시 RGB 텍스트,
  별색(Separation) 채움, 미지 색공간(Lab), 다중 페이지 pages 기록
"""

from __future__ import annotations

import json
from pathlib import Path

import pikepdf

from core.preflight.checks.colorspace import ALLOWED, check_colorspace
from core.preflight.engine import CheckContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "samples" / "corpus"
CLEAN = ROOT / "data" / "samples" / "clean"
MANIFEST = ROOT / "data" / "samples" / "manifest.json"


def _run(path: Path):
    """체크 1회 실행 (컨텍스트는 반드시 닫는다 — Windows tmp_path 정리 대비)."""
    ctx = CheckContext(path)
    try:
        return check_colorspace(ctx)
    finally:
        ctx.close()


def _manifest_entries() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]


def _defect_params(entry: dict, defect_id: str) -> dict | None:
    for d in entry["defects"]:
        if d["id"] == defect_id:
            return d["params"]
    return None


# ---------------------------------------------------------------- 코퍼스 스팟


def test_corpus_colorspace_defects_detected():
    """colorspace 결함 파일 전부에서 warn + RGB 오브젝트 실측 기록."""
    files = sorted(CORPUS.glob("*colorspace*.pdf"))
    assert len(files) >= 1, "코퍼스에 colorspace 결함 파일이 있어야 함"
    for f in files:
        r = _run(f)
        assert r.status == CheckStatus.WARN, f"{f.name}: {r.status} / {r.detail}"
        assert r.measured["rgb_objects"], f.name
        assert r.pages == [0], f.name  # 코퍼스 결함은 전부 1페이지 파일
        assert r.required["allowed"] == ALLOWED
        assert r.autofix.available is False
        assert "ICC" in r.autofix.note  # 본개발 변환 예정 고지


def test_corpus_both_defect_modes_caught():
    """주입기의 2모드(RGB 이미지 XObject / rg 채움)를 각각 kind로 구분해 잡는다."""
    seen_modes = set()
    for entry in _manifest_entries():
        params = _defect_params(entry, "colorspace")
        if params is None:
            continue
        mode = params["mode"]
        seen_modes.add(mode)
        r = _run(ROOT / entry["file"])
        assert r.status == CheckStatus.WARN, entry["file"]
        kinds = {o["kind"] for o in r.measured["rgb_objects"]}
        if mode == "image":
            assert "image" in kinds, entry["file"]
            assert any(
                o["kind"] == "image" and "RGB" in o["space"] for o in r.measured["rgb_objects"]
            ), entry["file"]
        else:  # fill
            assert "fill" in kinds, entry["file"]
    assert seen_modes == {"image", "fill"}, "코퍼스에 두 모드가 모두 있어야 함"


def test_clean_samples_pass():
    """정상 샘플 5종: 전부 pass + RGB 실측 0건."""
    files = sorted(CLEAN.glob("*.pdf"))
    assert len(files) >= 2
    for f in files:
        r = _run(f)
        assert r.status == CheckStatus.PASS, f"{f.name}: {r.status} / {r.detail}"
        assert r.measured["rgb_objects"] == []
        assert r.pages == []


def test_corpus_non_colorspace_files_silent():
    """colorspace 결함이 없는 코퍼스 45종(정상 10 + 타결함): 전부 pass.
    Separation:CutContour 칼선·투명도·저해상도 등 다른 결함에 오탐하지 않는다."""
    checked = 0
    for entry in _manifest_entries():
        if _defect_params(entry, "colorspace") is not None:
            continue
        checked += 1
        r = _run(ROOT / entry["file"])
        assert r.status == CheckStatus.PASS, f"{entry['file']}: {r.status} / {r.detail}"
        assert r.measured["rgb_objects"] == []
    assert checked >= 2


# ---------------------------------------------------------------- fixture 헬퍼


def _new_page(pdf: pikepdf.Pdf, content: bytes, resources: pikepdf.Dictionary | None = None):
    """100×100pt 페이지 + 콘텐츠 스트림 + (선택) 리소스."""
    page = pdf.add_blank_page(page_size=(100, 100))
    page.obj["/Contents"] = pdf.make_stream(content)
    if resources is not None:
        page.obj["/Resources"] = resources
    return page


def _save(pdf: pikepdf.Pdf, tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    pdf.save(p)
    pdf.close()
    return p


def _make_image_xobject(pdf: pikepdf.Pdf, colorspace, width=2, height=2, bits=8, data=b"\x00" * 16):
    img = pdf.make_stream(data)
    img["/Type"] = pikepdf.Name("/XObject")
    img["/Subtype"] = pikepdf.Name("/Image")
    img["/Width"] = width
    img["/Height"] = height
    img["/BitsPerComponent"] = bits
    img["/ColorSpace"] = colorspace
    return pdf.make_indirect(img)


# ---------------------------------------------------------------- 경계 fixture


def test_icc_rgb_image_warns(tmp_path):
    """ICC 기반 RGB(N=3) 이미지도 RGB 계열로 판정 → warn."""
    pdf = pikepdf.new()
    icc = pdf.make_indirect(pdf.make_stream(b"\x00" * 8))
    icc["/N"] = 3
    cs = pikepdf.Array([pikepdf.Name("/ICCBased"), icc])
    img = _make_image_xobject(pdf, cs, data=b"\x00" * 12)
    res = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im1=img))
    _new_page(pdf, b"q 50 0 0 50 25 25 cm /Im1 Do Q", res)
    p = _save(pdf, tmp_path, "icc_rgb.pdf")

    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert {"kind": "image", "page": 0, "space": "ICC-RGB"} in r.measured["rgb_objects"]


def test_indexed_rgb_image_warns(tmp_path):
    """Indexed(DeviceRGB) 팔레트 이미지 → RGB 계열 → warn."""
    pdf = pikepdf.new()
    cs = pikepdf.Array(
        [
            pikepdf.Name("/Indexed"),
            pikepdf.Name("/DeviceRGB"),
            1,
            pikepdf.String(b"\x00\x00\x00\xff\xff\xff"),
        ]
    )
    img = _make_image_xobject(pdf, cs, bits=1, data=b"\x00\x00")
    res = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im1=img))
    _new_page(pdf, b"q 50 0 0 50 25 25 cm /Im1 Do Q", res)
    p = _save(pdf, tmp_path, "indexed_rgb.pdf")

    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert {"kind": "image", "page": 0, "space": "Indexed(DeviceRGB)"} in r.measured["rgb_objects"]


def test_rgb_stroke_warns(tmp_path):
    """RG(RGB 스트로크)만 있어도 warn — 채움·이미지 외 경로도 커버."""
    pdf = pikepdf.new()
    _new_page(pdf, b"1 0 0 RG 2 w 10 10 m 90 90 l S")
    p = _save(pdf, tmp_path, "rgb_stroke.pdf")

    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert {"kind": "stroke", "page": 0, "space": "DeviceRGB"} in r.measured["rgb_objects"]


def test_invisible_rgb_text_passes(tmp_path):
    """렌더 모드 3(비표시) 텍스트의 RGB fill 은 인쇄에 안 나옴 → pass."""
    pdf = pikepdf.new()
    _new_page(pdf, b"BT /F1 12 Tf 1 0 0 rg 3 Tr 10 10 Td (Hi) Tj ET")
    p = _save(pdf, tmp_path, "invisible_rgb_text.pdf")

    r = _run(p)
    assert r.status == CheckStatus.PASS, r.detail


def test_visible_rgb_text_warns(tmp_path):
    """같은 RGB fill 이라도 렌더 모드 0(표시) 텍스트면 warn — 모드 3과의 경계쌍."""
    pdf = pikepdf.new()
    _new_page(pdf, b"BT /F1 12 Tf 1 0 0 rg 0 Tr 10 10 Td (Hi) Tj ET")
    p = _save(pdf, tmp_path, "visible_rgb_text.pdf")

    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert {"kind": "text", "page": 0, "space": "DeviceRGB"} in r.measured["rgb_objects"]


def test_separation_and_gray_pass(tmp_path):
    """별색(Separation) 채움 + 그레이 채움만 있는 파일 → pass."""
    pdf = pikepdf.new()
    sep = pikepdf.Array(
        [pikepdf.Name("/Separation"), pikepdf.Name("/PANTONE-123"), pikepdf.Name("/DeviceCMYK")]
    )
    res = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(CS0=sep))
    _new_page(pdf, b"/CS0 cs 1 scn 0 0 50 50 re f 0 g 10 10 30 30 re f", res)
    p = _save(pdf, tmp_path, "separation_gray.pdf")

    r = _run(p)
    assert r.status == CheckStatus.PASS, r.detail
    assert "Separation:PANTONE-123" in r.measured["spaces_seen"]


def test_unknown_space_uncertain(tmp_path):
    """Lab 등 판별 불가 색공간만 있으면 uncertain (에스컬레이션 대상)."""
    pdf = pikepdf.new()
    lab = pikepdf.Array([pikepdf.Name("/Lab"), pikepdf.Dictionary()])
    res = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(L0=lab))
    _new_page(pdf, b"/L0 cs 0.5 0 0 scn 0 0 40 40 re f", res)
    p = _save(pdf, tmp_path, "lab_fill.pdf")

    r = _run(p)
    assert r.status == CheckStatus.UNCERTAIN
    assert any(o["space"] == "Lab" for o in r.measured["unknown_objects"])


def test_multipage_pages_field(tmp_path):
    """페이지 0은 CMYK, 페이지 1만 rg 채움 → warn + pages == [1]."""
    pdf = pikepdf.new()
    _new_page(pdf, b"0 0 0 1 k 0 0 50 50 re f")
    _new_page(pdf, b"1 0 0 rg 0 0 50 50 re f")
    p = _save(pdf, tmp_path, "multipage_rgb.pdf")

    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.pages == [1]
    assert r.measured["rgb_objects"] == [{"kind": "fill", "page": 1, "space": "DeviceRGB"}]


def test_rgb_takes_precedence_over_unknown(tmp_path):
    """RGB 와 미지 색공간이 함께 있으면 warn (고지 우선) — 미지 목록은 실측에 보존."""
    pdf = pikepdf.new()
    lab = pikepdf.Array([pikepdf.Name("/Lab"), pikepdf.Dictionary()])
    res = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(L0=lab))
    _new_page(pdf, b"/L0 cs 0.5 0 0 scn 0 0 40 40 re f 1 0 0 rg 50 50 40 40 re f", res)
    p = _save(pdf, tmp_path, "rgb_plus_lab.pdf")

    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["rgb_objects"]
    assert r.measured["unknown_objects"]
