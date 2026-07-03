import pikepdf
import pytest
from reportlab.lib.colors import CMYKColor
from reportlab.pdfgen import canvas

from core.autofix.extend_bleed import extend_bleed
from core.preflight.engine import PT_PER_MM, pt_to_mm


@pytest.fixture
def no_bleed_pdf(tmp_path):
    """bleed 없는 90x90mm PDF (MediaBox == TrimBox)."""
    w = h = 90 * PT_PER_MM
    p = tmp_path / "no_bleed.pdf"
    c = canvas.Canvas(str(p), pagesize=(w, h))
    c.setFillColor(CMYKColor(0, 0.8, 0.9, 0))
    c.rect(0, 0, w, h, fill=1, stroke=0)
    c.setFillColor(CMYKColor(0, 0, 0, 1))
    c.drawString(w / 2 - 30, h / 2, "STICKER")
    c.save()
    with pikepdf.open(p, allow_overwriting_input=True) as pdf:
        pdf.pages[0]["/TrimBox"] = pikepdf.Array([0, 0, w, h])
        pdf.save(p)
    return p


def test_extend_bleed_adds_3mm(no_bleed_pdf, tmp_path):
    out = tmp_path / "fixed.pdf"
    result = extend_bleed(no_bleed_pdf, out, bleed_mm=3.0, preview_dir=tmp_path / "pv")

    with pikepdf.open(out) as pdf:
        page = pdf.pages[0]
        media = [float(v) for v in page["/MediaBox"]]
        trim = [float(v) for v in page["/TrimBox"]]
    # 재단 크기 보존 (±0.5mm)
    assert abs(pt_to_mm(trim[2] - trim[0]) - 90) < 0.5
    assert abs(pt_to_mm(trim[3] - trim[1]) - 90) < 0.5
    # 사방 3mm bleed
    for gap in (trim[0] - media[0], trim[1] - media[1], media[2] - trim[2], media[3] - trim[3]):
        assert abs(pt_to_mm(gap) - 3.0) < 0.2

    assert len(result["previews"]) == 1
    for key in ("before", "after"):
        assert (tmp_path / "pv").exists()


def test_original_untouched(no_bleed_pdf, tmp_path):
    before = no_bleed_pdf.read_bytes()
    extend_bleed(no_bleed_pdf, tmp_path / "fixed.pdf")
    assert no_bleed_pdf.read_bytes() == before


def test_fixed_file_passes_preflight_with_dieline(tmp_path):
    """코퍼스 실파일: 칼선 있는 스티커의 bleed 결함 → 보정 후 bleed·dieline·resolution 전부 통과.

    회귀 방지 대상: (1) 래스터화로 칼선 별색이 소실되던 문제,
    (2) 반올림 오차로 유효 해상도가 299.9dpi로 측정돼 warn이 붙던 문제.
    """
    from core.orchestrator.session import PROJECT_ROOT
    from core.preflight.engine import OrderContext, run_preflight
    from core.preflight.report import CheckStatus

    src = PROJECT_ROOT / "data" / "samples" / "corpus" / "single_03_sticker_bleed.pdf"
    assert src.exists(), "make gen-samples 필요"

    out = tmp_path / "fixed.pdf"
    result = extend_bleed(src, out, bleed_mm=3.0, preview_dir=tmp_path / "pv")
    assert result["dieline_preserved"] is True

    report = run_preflight(out, OrderContext(product="sticker", size_mm=(90, 90), page_count=1))
    by = {r.check_id: r for r in report.results}
    assert by["bleed"].status == CheckStatus.PASS, by["bleed"]
    assert by["dieline"].status == CheckStatus.PASS, by["dieline"]
    assert by["resolution"].status == CheckStatus.PASS, by["resolution"]
    assert by["page_size"].status == CheckStatus.PASS, by["page_size"]
    assert report.gate_ok, [r for r in report.results if r.status != CheckStatus.PASS]
