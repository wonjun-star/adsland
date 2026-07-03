"""bleed 체크 테스트 — 코퍼스 스팟 테스트 + 임계 근처 합성 fixture.

코퍼스의 물리적 사실:
- 정상 파일: MediaBox = trim + 사방 3mm, TrimBox = 3mm 인셋 → 여백 정확히 3.0mm.
- bleed 결함 파일: bleed_mm=0으로 생성 → MediaBox==TrimBox → 여백 0mm.
"""

from __future__ import annotations

import math
from pathlib import Path

import pikepdf
import pytest

from core.preflight.checks.bleed import check_bleed
from core.preflight.engine import CheckContext, mm_to_pt
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "samples" / "corpus"
CLEAN = ROOT / "data" / "samples" / "clean"


def _run(path: Path):
    ctx = CheckContext(path)
    try:
        return check_bleed(ctx)
    finally:
        ctx.close()


def _make_pdf(path: Path, pages_spec) -> Path:
    """박스만 있는 최소 PDF 생성 (bleed 체크는 박스 좌표만 본다).

    pages_spec: 페이지별 (trim_w_mm, trim_h_mm, insets) 목록.
    insets = (left, bottom, right, top) mm — None이면 TrimBox를 넣지 않는다.
    """
    pdf = pikepdf.new()
    for trim_w, trim_h, insets in pages_spec:
        tw, th = mm_to_pt(trim_w), mm_to_pt(trim_h)
        if insets is None:
            pdf.add_blank_page(page_size=(tw, th))  # TrimBox 미설정
            continue
        left, bottom, right, top = (mm_to_pt(v) for v in insets)
        page = pdf.add_blank_page(page_size=(tw + left + right, th + bottom + top))
        page.obj["/TrimBox"] = pikepdf.Array([left, bottom, left + tw, bottom + th])
    pdf.save(path)
    return path


# ---------------------------------------------------------------------------
# 코퍼스 스팟 테스트
# ---------------------------------------------------------------------------

_BLEED_DEFECT_FILES = sorted(CORPUS.glob("*bleed*.pdf"))
_CLEAN_FILES = sorted(CLEAN.glob("*.pdf"))


def test_corpus_files_exist():
    """glob 전제 확인 — 결함 파일 ≥1, 정상 파일 ≥2."""
    assert len(_BLEED_DEFECT_FILES) >= 1
    assert len(_CLEAN_FILES) >= 2


@pytest.mark.parametrize("path", _BLEED_DEFECT_FILES, ids=lambda p: p.name)
def test_corpus_bleed_defect_detected(path):
    """bleed 결함 주입 파일: 여백 0mm → fail + extend_bleed autofix."""
    r = _run(path)
    assert r.status == CheckStatus.FAIL
    assert r.autofix.available is True
    assert r.autofix.fix_id == "extend_bleed"
    assert r.measured["min_mm"] < 2.9
    assert r.required == {"min_mm": 3.0}
    assert r.pages  # 문제 페이지가 기록되어야 한다


@pytest.mark.parametrize("path", _CLEAN_FILES, ids=lambda p: p.name)
def test_clean_files_pass(path):
    """정상 파일: 사방 정확히 3.0mm → pass, 문제 페이지 없음."""
    r = _run(path)
    assert r.status == CheckStatus.PASS
    assert r.pages == []
    assert math.isclose(r.measured["min_mm"], 3.0, abs_tol=0.05)
    for side in ("left", "right", "top", "bottom"):
        assert math.isclose(r.measured["insets_mm"][side], 3.0, abs_tol=0.05)


# ---------------------------------------------------------------------------
# 임계 근처 합성 fixture (tmp_path)
# ---------------------------------------------------------------------------

def test_exact_3mm_passes(tmp_path):
    p = _make_pdf(tmp_path / "exact.pdf", [(90, 50, (3.0, 3.0, 3.0, 3.0))])
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert math.isclose(r.measured["min_mm"], 3.0, abs_tol=0.01)


def test_within_tolerance_passes(tmp_path):
    """2.95mm — 기준 3.0mm이지만 허용오차 -0.1mm 안 → pass."""
    p = _make_pdf(tmp_path / "tol.pdf", [(90, 50, (2.95, 2.95, 2.95, 2.95))])
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert math.isclose(r.measured["min_mm"], 2.95, abs_tol=0.01)


def test_below_tolerance_fails(tmp_path):
    """2.8mm — 허용오차 밖 → fail + autofix."""
    p = _make_pdf(tmp_path / "below.pdf", [(90, 50, (2.8, 2.8, 2.8, 2.8))])
    r = _run(p)
    assert r.status == CheckStatus.FAIL
    assert r.autofix.available and r.autofix.fix_id == "extend_bleed"
    assert math.isclose(r.measured["min_mm"], 2.8, abs_tol=0.01)


def test_one_side_short_fails_with_direction(tmp_path):
    """오른쪽만 1mm — 방향별 실측이 정확해야 한다."""
    p = _make_pdf(tmp_path / "oneside.pdf", [(90, 50, (3.0, 3.0, 1.0, 3.0))])
    r = _run(p)
    assert r.status == CheckStatus.FAIL
    ins = r.measured["insets_mm"]
    assert math.isclose(ins["right"], 1.0, abs_tol=0.01)
    assert math.isclose(ins["left"], 3.0, abs_tol=0.01)
    assert math.isclose(ins["bottom"], 3.0, abs_tol=0.01)
    assert math.isclose(ins["top"], 3.0, abs_tol=0.01)
    assert math.isclose(r.measured["min_mm"], 1.0, abs_tol=0.01)


def test_no_trimbox_fails_with_autofix(tmp_path):
    """TrimBox 부재(실무 흔함): trim==media 간주 → 여백 0 → fail + autofix."""
    p = _make_pdf(tmp_path / "notrim.pdf", [(96, 56, None)])
    r = _run(p)
    assert r.status == CheckStatus.FAIL
    assert r.autofix.available and r.autofix.fix_id == "extend_bleed"
    assert math.isclose(r.measured["min_mm"], 0.0, abs_tol=0.01)
    assert "TrimBox" in r.detail  # 간주 사실이 detail에 명시되어야 한다


def test_multipage_min_across_pages(tmp_path):
    """2페이지 중 2번째만 top 0.5mm — pages=[1], 방향별 최소는 전 페이지 기준."""
    p = _make_pdf(
        tmp_path / "multi.pdf",
        [
            (90, 50, (3.0, 3.0, 3.0, 3.0)),
            (90, 50, (3.0, 3.0, 3.0, 0.5)),
        ],
    )
    r = _run(p)
    assert r.status == CheckStatus.FAIL
    assert r.pages == [1]
    assert math.isclose(r.measured["insets_mm"]["top"], 0.5, abs_tol=0.01)
    assert math.isclose(r.measured["insets_mm"]["left"], 3.0, abs_tol=0.01)
    assert math.isclose(r.measured["min_mm"], 0.5, abs_tol=0.01)


def test_bleedbox_narrower_than_media_uses_wider(tmp_path):
    """BleedBox가 TrimBox와 같아도 MediaBox가 3mm 넓으면 넓은 쪽 기준 → pass."""
    p = _make_pdf(tmp_path / "narrowbleed.pdf", [(90, 50, (3.0, 3.0, 3.0, 3.0))])
    with pikepdf.open(p, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.obj["/BleedBox"] = pikepdf.Array(list(page.obj["/TrimBox"]))
        pdf.save(p)
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert math.isclose(r.measured["min_mm"], 3.0, abs_tol=0.01)
