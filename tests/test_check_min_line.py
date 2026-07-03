"""min_line 체크 테스트.

- 코퍼스 스팟: min_line 결함 파일(0.05pt K100 선)에서 warn 검출,
  정상 파일에서 pass (sticker/label의 1.0pt Separation 칼선은 제외 대상 확인 포함).
- 임계 근처: pikepdf로 직접 만든 fixture — 0.25pt(경계)는 pass, 0.24pt는 warn,
  0pt 헤어라인 warn, CTM 스케일 반영 확인, Separation 얇은 선 제외 확인.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from core.preflight.checks.min_line import MIN_LINE_PT, check_min_line
from core.preflight.engine import CheckContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "data" / "samples" / "corpus"
CLEAN_DIR = ROOT / "data" / "samples" / "clean"

# 파일명에 결함 id가 들어있다 — glob으로 수집
DEFECT_FILES = sorted(CORPUS_DIR.glob("*min_line*.pdf"))
CLEAN_FILES = sorted(CLEAN_DIR.glob("clean_*.pdf"))


def _run(pdf_path: Path):
    ctx = CheckContext(pdf_path)
    try:
        return check_min_line(ctx)
    finally:
        ctx.close()


# ---------------------------------------------------------------- 코퍼스 스팟

@pytest.mark.skipif(not DEFECT_FILES, reason="코퍼스 min_line 파일 없음")
@pytest.mark.parametrize("pdf_path", DEFECT_FILES, ids=lambda p: p.name)
def test_corpus_defect_detected(pdf_path: Path):
    """0.05pt 결함 선이 있는 코퍼스 파일 → warn + 실측값 기록."""
    r = _run(pdf_path)
    assert r.status == CheckStatus.WARN
    assert r.measured["min_width_pt"] is not None
    assert r.measured["min_width_pt"] < MIN_LINE_PT
    # 주입기는 0.05pt 선을 넣는다
    assert r.measured["min_width_pt"] == pytest.approx(0.05, abs=0.01)
    assert r.measured["thin_stroke_count"] >= 1
    assert r.pages, "문제 페이지가 기록되어야 한다"
    assert r.required == {"min_pt": 0.25}


@pytest.mark.skipif(len(CLEAN_FILES) < 2, reason="정상 샘플 부족")
@pytest.mark.parametrize("pdf_path", CLEAN_FILES, ids=lambda p: p.name)
def test_clean_files_pass(pdf_path: Path):
    """정상 파일 → pass. 유일한 스트로크는 칼선(Separation 1.0pt)뿐이며 제외된다."""
    r = _run(pdf_path)
    assert r.status == CheckStatus.PASS
    assert r.measured["thin_stroke_count"] == 0
    assert r.pages == []


def test_clean_sticker_dieline_excluded():
    """sticker 정상 파일: Separation 칼선은 인쇄 스트로크 집계에서 빠져야 한다."""
    path = CLEAN_DIR / "clean_sticker.pdf"
    if not path.exists():
        pytest.skip("clean_sticker.pdf 없음")
    r = _run(path)
    assert r.status == CheckStatus.PASS
    # 칼선(1.0pt) 하나뿐이므로 인쇄 스트로크는 0개 → min_width_pt는 None
    assert r.measured["stroke_count"] == 0
    assert r.measured["min_width_pt"] is None


# ---------------------------------------------------------- 임계 근처 fixture

def _make_pdf(path: Path, ops: str, with_sep_cs: bool = False) -> Path:
    """단일 페이지 PDF를 pikepdf로 직접 생성. ops는 콘텐츠 스트림 문자열."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    resources = pikepdf.Dictionary()
    if with_sep_cs:
        # Separation:CutContour 색공간 (tint transform은 형식만 갖춘 Type2 함수)
        func = pdf.make_indirect(
            pikepdf.Dictionary(
                FunctionType=2,
                Domain=[0, 1],
                C0=[0, 0, 0, 0],
                C1=[0, 0, 0, 1],
                N=1,
            )
        )
        sep = pikepdf.Array(
            [pikepdf.Name("/Separation"), pikepdf.Name("/CutContour"), pikepdf.Name("/DeviceCMYK"), func]
        )
        resources["/ColorSpace"] = pikepdf.Dictionary(CS0=sep)
    page.obj["/Resources"] = resources
    page.contents_add(pikepdf.Stream(pdf, ops.encode("ascii")))
    pdf.save(path)
    return path


def test_boundary_exact_025pt_passes(tmp_path: Path):
    """정확히 0.25pt 선 → 기준 충족, pass."""
    p = _make_pdf(tmp_path / "b025.pdf", "q 0 0 0 1 K 0.25 w 10 100 m 190 100 l S Q")
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert r.measured["min_width_pt"] == pytest.approx(0.25)


def test_boundary_024pt_warns(tmp_path: Path):
    """0.24pt 선 → 기준 미달, warn."""
    p = _make_pdf(tmp_path / "b024.pdf", "q 0 0 0 1 K 0.24 w 10 100 m 190 100 l S Q")
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["min_width_pt"] == pytest.approx(0.24)
    assert r.pages == [0]


def test_zero_width_hairline_warns(tmp_path: Path):
    """0pt 헤어라인 → warn (0 포함 규칙)."""
    p = _make_pdf(tmp_path / "hair.pdf", "q 0 0 0 1 K 0 w 10 100 m 190 100 l S Q")
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["min_width_pt"] == pytest.approx(0.0)


def test_ctm_scale_applied(tmp_path: Path):
    """0.5pt 선폭이라도 CTM 0.2배 축소면 유효 폭 0.1pt → warn."""
    p = _make_pdf(
        tmp_path / "ctm.pdf",
        "q 0.2 0 0 0.2 0 0 cm 0 0 0 1 K 0.5 w 10 100 m 190 100 l S Q",
    )
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["min_width_pt"] == pytest.approx(0.1, abs=1e-6)


def test_thin_separation_stroke_excluded(tmp_path: Path):
    """얇은(0.05pt) 선이라도 Separation(칼선)이면 제외 → pass."""
    ops = "q /CS0 CS 1 SCN 0.05 w 10 100 m 190 100 l S Q"
    p = _make_pdf(tmp_path / "sep.pdf", ops, with_sep_cs=True)
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert r.measured["stroke_count"] == 0


def test_no_strokes_at_all_passes(tmp_path: Path):
    """스트로크가 아예 없어도 pass."""
    p = _make_pdf(tmp_path / "none.pdf", "q 0 0 0 0.3 k 10 10 180 180 re f Q")
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert r.measured["min_width_pt"] is None
    assert r.measured["stroke_count"] == 0


def test_multipage_reports_bad_pages_only(tmp_path: Path):
    """2페이지 중 2페이지째만 얇은 선 → pages=[1]."""
    pdf = pikepdf.new()
    p1 = pdf.add_blank_page(page_size=(200, 200))
    p1.contents_add(pikepdf.Stream(pdf, b"q 0 0 0 1 K 1 w 10 100 m 190 100 l S Q"))
    p2 = pdf.add_blank_page(page_size=(200, 200))
    p2.contents_add(pikepdf.Stream(pdf, b"q 0 0 0 1 K 0.05 w 10 100 m 190 100 l S Q"))
    path = tmp_path / "multi.pdf"
    pdf.save(path)
    r = _run(path)
    assert r.status == CheckStatus.WARN
    assert r.pages == [1]
    assert r.measured["min_width_pt"] == pytest.approx(0.05)
    assert r.measured["stroke_count"] == 2
