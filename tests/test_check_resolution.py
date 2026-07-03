"""resolution 체크 테스트.

- 코퍼스 스팟: 파일명에 resolution이 든 결함 파일(주입 96dpi)에서 fail 검출,
  정상 파일(320dpi 사진)에서 pass.
- 임계 근처: pikepdf로 tmp_path에 직접 fixture를 만들어 300/150 경계,
  이미지 없음, 5pt 미만 장식 이미지 무시, 다중 페이지를 검증.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from core.preflight.checks.resolution import check_resolution
from core.preflight.engine import CheckContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "samples" / "corpus"
CLEAN = ROOT / "data" / "samples" / "clean"

# 파일명에 결함 id가 들어있다 (예: single_04_poster_resolution.pdf)
DEFECT_FILES = sorted(CORPUS.glob("*resolution*.pdf"))
CLEAN_FILES = sorted(CLEAN.glob("clean_*.pdf"))


def _run(pdf_path: Path):
    ctx = CheckContext(pdf_path)
    try:
        return check_resolution(ctx)
    finally:
        ctx.close()


# ---------------------------------------------------------------- 코퍼스 스팟


@pytest.mark.skipif(not DEFECT_FILES, reason="코퍼스 미생성")
@pytest.mark.parametrize("pdf", DEFECT_FILES, ids=lambda p: p.name)
def test_corpus_defect_detected(pdf: Path):
    """주입기가 96dpi로 낮춘 이미지 → 150 미만이므로 fail."""
    r = _run(pdf)
    assert r.status == CheckStatus.FAIL
    assert r.measured["min_dpi"] < 150
    # 주입 파라미터(96dpi)와 근사해야 한다 (픽셀 정수 반올림 오차 허용)
    assert r.measured["min_dpi"] == pytest.approx(96.0, abs=3.0)
    assert r.pages, "문제 페이지가 기록되어야 한다"
    assert r.required == {"pass_dpi": 300, "fail_below": 150}


@pytest.mark.skipif(len(CLEAN_FILES) < 2, reason="정상 샘플 미생성")
@pytest.mark.parametrize("pdf", CLEAN_FILES, ids=lambda p: p.name)
def test_clean_pass(pdf: Path):
    """정상 파일(320dpi CMYK 사진 1개)은 침묵해야 한다."""
    r = _run(pdf)
    assert r.status == CheckStatus.PASS
    assert r.measured["min_dpi"] is not None
    assert r.measured["min_dpi"] >= 300
    assert r.pages == []


# ------------------------------------------------------------ 직접 만든 fixture


def _make_pdf(path: Path, pages: list[list[tuple[int, int, float, float]]]) -> Path:
    """페이지별 이미지 목록 (w_px, h_px, placed_w_pt, placed_h_pt)로 최소 PDF 생성."""
    pdf = pikepdf.new()
    for imgs in pages:
        page = pdf.add_blank_page(page_size=(595, 842))
        xobjs = pikepdf.Dictionary()
        ops = []
        for i, (w, h, pw, ph) in enumerate(imgs):
            st = pikepdf.Stream(pdf, b"\x80" * (w * h))
            st.Type = pikepdf.Name("/XObject")
            st.Subtype = pikepdf.Name("/Image")
            st.Width = w
            st.Height = h
            st.ColorSpace = pikepdf.Name("/DeviceGray")
            st.BitsPerComponent = 8
            xobjs[f"/Im{i}"] = st
            ops.append(f"q {pw} 0 0 {ph} 40 40 cm /Im{i} Do Q")
        page.Resources = pikepdf.Dictionary(XObject=xobjs)
        page.Contents = pikepdf.Stream(pdf, " ".join(ops).encode())
    pdf.save(path)
    return path


def test_exactly_300dpi_passes(tmp_path: Path):
    # 300px를 72pt(=1inch)에 배치 → 정확히 300dpi → pass
    pdf = _make_pdf(tmp_path / "dpi300.pdf", [[(300, 300, 72.0, 72.0)]])
    r = _run(pdf)
    assert r.status == CheckStatus.PASS
    assert r.measured["min_dpi"] == pytest.approx(300.0, abs=0.1)


def test_between_150_and_300_warns(tmp_path: Path):
    # 200px/1inch = 200dpi → warn
    pdf = _make_pdf(tmp_path / "dpi200.pdf", [[(200, 200, 72.0, 72.0)]])
    r = _run(pdf)
    assert r.status == CheckStatus.WARN
    assert r.measured["min_dpi"] == pytest.approx(200.0, abs=0.1)
    assert r.pages == [0]


def test_exactly_150dpi_warns(tmp_path: Path):
    # 150dpi는 '150~300' 구간의 하한 → warn (fail은 150 미만부터)
    pdf = _make_pdf(tmp_path / "dpi150.pdf", [[(150, 150, 72.0, 72.0)]])
    r = _run(pdf)
    assert r.status == CheckStatus.WARN


def test_below_150dpi_fails(tmp_path: Path):
    # 149px/1inch = 149dpi → fail
    pdf = _make_pdf(tmp_path / "dpi149.pdf", [[(149, 149, 72.0, 72.0)]])
    r = _run(pdf)
    assert r.status == CheckStatus.FAIL
    assert r.measured["min_dpi"] == pytest.approx(149.0, abs=0.1)


def test_min_of_anisotropic_placement(tmp_path: Path):
    # 300x300px를 72x144pt에 배치 → 세로 유효 150dpi (낮은 쪽 기준) → warn
    pdf = _make_pdf(tmp_path / "aniso.pdf", [[(300, 300, 72.0, 144.0)]])
    r = _run(pdf)
    assert r.status == CheckStatus.WARN
    assert r.measured["min_dpi"] == pytest.approx(150.0, abs=0.1)


def test_no_images_passes_with_detail(tmp_path: Path):
    pdf = _make_pdf(tmp_path / "noimg.pdf", [[]])
    r = _run(pdf)
    assert r.status == CheckStatus.PASS
    assert r.measured["min_dpi"] is None
    assert r.measured["images"] == []
    assert "이미지 없음" in r.detail


def test_tiny_decorative_image_ignored(tmp_path: Path):
    # 4pt 배치(<5pt) 저해상도 아이콘은 무시 → 나머지 300dpi 이미지만으로 pass
    pdf = _make_pdf(
        tmp_path / "deco.pdf",
        [[(10, 10, 4.0, 4.0), (300, 300, 72.0, 72.0)]],
    )
    r = _run(pdf)
    assert r.status == CheckStatus.PASS
    assert len(r.measured["images"]) == 1  # 장식 이미지는 목록에서도 제외


def test_multipage_records_problem_page(tmp_path: Path):
    # 1페이지는 300dpi, 2페이지는 100dpi → fail, pages=[1]
    pdf = _make_pdf(
        tmp_path / "multi.pdf",
        [[(300, 300, 72.0, 72.0)], [(100, 100, 72.0, 72.0)]],
    )
    r = _run(pdf)
    assert r.status == CheckStatus.FAIL
    assert r.pages == [1]
    assert r.measured["min_dpi"] == pytest.approx(100.0, abs=0.1)
    assert len(r.measured["images"]) == 2


def test_internal_error_becomes_uncertain(tmp_path: Path):
    # 이벤트 수집이 터져도 예외가 밖으로 나가지 않고 uncertain이어야 한다
    pdf = _make_pdf(tmp_path / "ok.pdf", [[(300, 300, 72.0, 72.0)]])
    ctx = CheckContext(pdf)
    try:
        ctx.content_events = lambda i: (_ for _ in ()).throw(RuntimeError("boom"))
        r = check_resolution(ctx)
    finally:
        ctx.close()
    assert r.status == CheckStatus.UNCERTAIN
    assert "boom" in r.detail
