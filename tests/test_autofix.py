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
