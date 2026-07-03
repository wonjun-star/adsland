"""trim_safety 체크 테스트.

- 코퍼스 스팟: trim_safety 결함 파일(5종)에서 uncertain 검출,
  정상/타결함 파일 전체에서 pass (오탐 0).
- 임계 근처: reportlab+pikepdf로 tmp_path에 직접 fixture 생성
  (2mm 침범 / 4mm 안전 / 재단선 밖 / TrimBox 부재 / 다중 페이지).
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from reportlab.lib.units import mm as MM_PT  # 1mm = 72/25.4 pt
from reportlab.pdfgen import canvas as rl_canvas

from core.preflight.checks.trim_safety import SAFE_MARGIN_MM, check_trim_safety
from core.preflight.engine import CheckContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "samples" / "corpus"
CLEAN = ROOT / "data" / "samples" / "clean"

BLEED_MM = 3.0  # fixture의 bleed 폭 (코퍼스와 동일)


def _run(path: Path):
    """체크 1회 실행 (컨텍스트 확실히 닫기)."""
    ctx = CheckContext(path)
    try:
        return check_trim_safety(ctx)
    finally:
        ctx.close()


def _make_pdf(
    path: Path,
    trim_w_mm: float = 90.0,
    trim_h_mm: float = 50.0,
    texts: list[tuple[int, float, float, str, float]] = (),
    with_trimbox: bool = True,
    n_pages: int = 1,
) -> Path:
    """MediaBox = trim + 사방 3mm bleed 인 fixture PDF.

    texts: (page, x_mm, y_mm, 문자열, 폰트크기pt) — x/y는 TrimBox 좌하 원점
    기준 베이스라인 위치(mm). 음수면 재단선 밖(bleed)으로 나간다.
    """
    b = BLEED_MM * MM_PT
    media_w = trim_w_mm * MM_PT + 2 * b
    media_h = trim_h_mm * MM_PT + 2 * b
    c = rl_canvas.Canvas(str(path), pagesize=(media_w, media_h))
    for p in range(n_pages):
        c.setFillColorCMYK(0, 0, 0, 1)
        for pg, x_mm, y_mm, s, size in texts:
            if pg != p:
                continue
            c.setFont("Helvetica", size)
            c.drawString(b + x_mm * MM_PT, b + y_mm * MM_PT, s)
        c.showPage()
    c.save()
    # reportlab은 TrimBox를 못 쓰므로 pikepdf로 후처리 (생성기와 동일 패턴)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            if with_trimbox:
                page["/TrimBox"] = pikepdf.Array(
                    [b, b, b + trim_w_mm * MM_PT, b + trim_h_mm * MM_PT]
                )
            page["/BleedBox"] = pikepdf.Array(page["/MediaBox"])
        pdf.save(path)
    return path


# ---------------------------------------------------------------- 코퍼스 스팟


def test_corpus_defect_files_detected():
    """trim_safety 결함 파일(1mm 인셋 텍스트)은 전부 uncertain으로 검출."""
    files = sorted(CORPUS.glob("*trim_safety*.pdf"))
    assert len(files) >= 1, "코퍼스에 trim_safety 결함 파일이 없음"
    for f in files:
        r = _run(f)
        assert r.status == CheckStatus.UNCERTAIN, f.name
        assert r.required == {"safe_margin_mm": SAFE_MARGIN_MM}
        vios = r.measured["violations"]
        assert vios, f.name
        assert 0 in r.pages, f.name
        # 주입기 GT: 재단선에서 1mm 안쪽 → 실측 이격 ≈ 1mm (< 3mm)
        worst = min(v["char_bbox_mm_from_trim"] for v in vios)
        assert 0.3 <= worst <= 1.5, f"{f.name}: {worst}mm"
        for v in vios:
            assert v["char_bbox_mm_from_trim"] < SAFE_MARGIN_MM


def test_corpus_clean_files_pass():
    """정상 파일(콘텐츠 인셋 5mm)은 전부 pass — 위반 0건."""
    files = sorted(CORPUS.glob("clean_*.pdf")) + sorted(CLEAN.glob("*.pdf"))
    assert len(files) >= 2, "정상 샘플이 부족함"
    for f in files:
        r = _run(f)
        assert r.status == CheckStatus.PASS, f"{f.name}: {r.detail}"
        assert r.measured["violation_count"] == 0, f.name
        assert r.pages == [], f.name


def test_corpus_other_defects_no_false_positive():
    """trim_safety 외 결함 파일에서 오탐 0 — 배경/밴드가 bleed까지 칠해져 있어도
    텍스트만 보므로 조용해야 한다. (2페이지 파일은 전 페이지 검사)"""
    files = [
        f for f in sorted(CORPUS.glob("*.pdf"))
        if "trim_safety" not in f.name and not f.name.startswith("clean_")
    ]
    assert len(files) >= 2
    for f in files:
        r = _run(f)
        assert r.status == CheckStatus.PASS, f"{f.name}: {r.detail}"


# ---------------------------------------------------------------- 임계 근처 fixture


def test_violation_at_2mm(tmp_path):
    """재단선 2mm 안쪽 텍스트 → 3mm 기준 미달 → uncertain (fail 아님)."""
    f = _make_pdf(tmp_path / "vio2.pdf", texts=[(0, 2.0, 2.0, "NEAR EDGE", 7.0)])
    r = _run(f)
    assert r.status == CheckStatus.UNCERTAIN
    vios = r.measured["violations"]
    assert vios and r.pages == [0]
    # 베이스라인이 2mm → 대문자 글리프 하단 이격 ≈ 2mm
    worst = min(v["char_bbox_mm_from_trim"] for v in vios)
    assert 1.5 <= worst <= 2.5, worst


def test_pass_at_4mm(tmp_path):
    """재단선 4mm 안쪽 텍스트 → 기준(3mm) 충족 → pass (임계 위쪽 근처)."""
    f = _make_pdf(tmp_path / "ok4.pdf", texts=[(0, 4.0, 4.0, "SAFE TEXT", 7.0)])
    r = _run(f)
    assert r.status == CheckStatus.PASS, r.detail
    assert r.measured["violation_count"] == 0


def test_text_outside_trim_negative_distance(tmp_path):
    """재단선 밖(bleed)으로 나간 텍스트 → 음수 이격으로 검출."""
    f = _make_pdf(tmp_path / "out.pdf", texts=[(0, -2.0, 25.0, "OVERFLOW", 7.0)])
    r = _run(f)
    assert r.status == CheckStatus.UNCERTAIN
    worst = min(v["char_bbox_mm_from_trim"] for v in r.measured["violations"])
    assert worst < 0, worst


def test_no_trimbox_is_uncertain(tmp_path):
    """TrimBox 없음 → 안전영역 기준 산출 불가 → uncertain (예외 아님)."""
    f = _make_pdf(
        tmp_path / "notrim.pdf",
        texts=[(0, 10.0, 10.0, "HELLO", 7.0)],
        with_trimbox=False,
    )
    r = _run(f)
    assert r.status == CheckStatus.UNCERTAIN
    assert r.measured.get("pages_without_trimbox") == [0]


def test_multipage_reports_only_bad_page(tmp_path):
    """2페이지 중 2쪽만 침범 → pages == [1], 1쪽은 침묵."""
    f = _make_pdf(
        tmp_path / "multi.pdf",
        n_pages=2,
        texts=[
            (0, 10.0, 10.0, "SAFE PAGE", 7.0),   # 1쪽: 10mm 안쪽 (안전)
            (1, 1.5, 1.5, "EDGE PAGE", 7.0),     # 2쪽: 1.5mm (침범)
        ],
    )
    r = _run(f)
    assert r.status == CheckStatus.UNCERTAIN
    assert r.pages == [1]
    assert all(v["page"] == 1 for v in r.measured["violations"])


def test_empty_page_passes(tmp_path):
    """텍스트가 아예 없는 페이지 → pass (위반 0건)."""
    f = _make_pdf(tmp_path / "empty.pdf", texts=[])
    r = _run(f)
    assert r.status == CheckStatus.PASS
    assert r.measured["violation_count"] == 0
