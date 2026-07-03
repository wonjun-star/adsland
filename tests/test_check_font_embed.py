"""font_embed 체크 테스트.

- 코퍼스 실파일 스팟: font_embed 결함 파일에서 검출 + 정상/타결함 파일에서 침묵
- tmp_path 합성 fixture: 미사용 폰트 무시(reportlab 프리앰블 케이스), 다중 페이지,
  Type3 내장 글리프, 리소스 미해결 키(uncertain), 텍스트 없음
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from core.preflight.checks.font_embed import check_font_embed
from core.preflight.engine import CheckContext
from core.preflight.report import CheckStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORPUS = PROJECT_ROOT / "data" / "samples" / "corpus"

EMBED_FONT = "VeraTest"


def _run(path: Path):
    ctx = CheckContext(path)
    try:
        return check_font_embed(ctx)
    finally:
        ctx.close()


def _corpus(pattern: str) -> list[Path]:
    files = sorted(CORPUS.glob(pattern))
    if not files:
        pytest.skip(f"코퍼스 파일 없음: {CORPUS / pattern}")
    return files


def _ensure_vera() -> None:
    """reportlab 동봉 TTF 등록 — 서브셋 임베딩(Type0 + FontFile2) 케이스용."""
    if EMBED_FONT not in pdfmetrics.getRegisteredFontNames():
        vera = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
        pdfmetrics.registerFont(TTFont(EMBED_FONT, str(vera)))


# ---------------------------------------------------------------- 코퍼스 스팟


def test_corpus_font_embed_defects_fail():
    """결함 파일(단일 2 + 복합 2)에서 미임베딩 Helvetica를 fail로 검출."""
    files = _corpus("*font_embed*.pdf")
    assert len(files) >= 1
    for f in files:
        r = _run(f)
        assert r.status == CheckStatus.FAIL, f"{f.name}: {r.status} / {r.detail}"
        bad = [x for x in r.measured["fonts"] if x["used"] and x["embedded"] is False]
        assert bad, f.name
        assert any("Helvetica" in x["name"] for x in bad), f"{f.name}: {bad}"
        assert r.pages == [0], f.name  # 코퍼스 font_embed 파일은 전부 1페이지
        assert r.measured["unembedded_used_font_count"] >= 1
        assert r.required == {"unembedded_used_font_count": 0}
        assert r.autofix.available is False


def test_corpus_clean_pass():
    """정상 파일: 텍스트는 있고(임베딩 Vera) 전부 통과 — 오탐 0."""
    files = _corpus("clean_*.pdf")
    assert len(files) >= 2
    for f in files:
        r = _run(f)
        assert r.status == CheckStatus.PASS, f"{f.name}: {r.status} / {r.detail}"
        used = [x for x in r.measured["fonts"] if x["used"]]
        assert used, f.name  # 본문 텍스트가 있으므로 사용 폰트가 존재해야 함
        assert all(x["embedded"] for x in used), f.name


def test_corpus_other_defects_pass():
    """font_embed 외 결함 파일(2페이지 page_count 포함)에서는 침묵 — 오탐 방지."""
    files = _corpus("single_01_*.pdf") + _corpus("*page_count*.pdf")
    for f in files:
        r = _run(f)
        assert r.status == CheckStatus.PASS, f"{f.name}: {r.status} / {r.detail}"


# ---------------------------------------------------------------- 합성 fixture


def test_unused_helvetica_in_resources_ignored(tmp_path):
    """리소스에 미임베딩 폰트가 있어도 표시에 안 쓰이면 무시 (reportlab 프리앰블 케이스)."""
    _ensure_vera()
    path = tmp_path / "unused_helv.pdf"
    c = Canvas(str(path), pagesize=(200, 200), initialFontName=EMBED_FONT)
    c.setFont(EMBED_FONT, 10)
    c.drawString(40, 100, "embedded only")
    c.showPage()
    c.save()
    # 미사용 base-14 Helvetica를 리소스에 강제로 끼워넣는다
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        fonts = page["/Resources"]["/Font"]
        fonts["/FUnused99"] = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Font"),
                Subtype=pikepdf.Name("/Type1"),
                BaseFont=pikepdf.Name("/Helvetica"),
            )
        )
        pdf.save(path)

    r = _run(path)
    assert r.status == CheckStatus.PASS, r.detail
    helv = [x for x in r.measured["fonts"] if x["name"] == "/Helvetica"]
    assert helv and helv[0]["used"] is False and helv[0]["embedded"] is False
    assert r.measured["unembedded_used_font_count"] == 0


def test_used_helvetica_fails_with_pages(tmp_path):
    """base-14 Helvetica로 실제 텍스트를 그리면 fail + 문제 페이지(0,1) 기록."""
    path = tmp_path / "used_helv.pdf"
    c = Canvas(str(path), pagesize=(200, 200))
    for line in ("page one", "page two"):
        c.setFont("Helvetica", 12)
        c.drawString(40, 100, line)
        c.showPage()
    c.save()

    r = _run(path)
    assert r.status == CheckStatus.FAIL, r.detail
    assert r.pages == [0, 1]
    bad = [x for x in r.measured["fonts"] if x["used"] and x["embedded"] is False]
    assert bad and bad[0]["name"] == "/Helvetica"


def test_mixed_embedded_and_unembedded_fails(tmp_path):
    """임베딩 폰트와 미임베딩 폰트가 섞여도 미임베딩 1종 때문에 fail."""
    _ensure_vera()
    path = tmp_path / "mixed.pdf"
    c = Canvas(str(path), pagesize=(200, 200), initialFontName=EMBED_FONT)
    c.setFont(EMBED_FONT, 10)
    c.drawString(40, 120, "embedded line")
    c.setFont("Courier", 10)  # base-14 — FontDescriptor 없음
    c.drawString(40, 100, "unembedded line")
    c.showPage()
    c.save()

    r = _run(path)
    assert r.status == CheckStatus.FAIL, r.detail
    names = {x["name"]: x for x in r.measured["fonts"] if x["used"]}
    assert "/Courier" in names and names["/Courier"]["embedded"] is False
    assert any(x["embedded"] for x in names.values())  # Vera는 임베딩으로 판정
    assert r.measured["unembedded_used_fonts"] == ["/Courier"]


def test_no_text_passes(tmp_path):
    """표시 텍스트가 전혀 없으면 pass (프리앰블 Tf만으로는 '사용' 아님)."""
    path = tmp_path / "no_text.pdf"
    c = Canvas(str(path), pagesize=(200, 200))
    c.setFillColorCMYK(0, 0, 0, 1)
    c.rect(20, 20, 160, 160, stroke=0, fill=1)
    c.showPage()
    c.save()

    r = _run(path)
    assert r.status == CheckStatus.PASS, r.detail
    assert r.measured["used_font_count"] == 0


def test_type3_font_counts_as_embedded(tmp_path):
    """Type3 폰트는 글리프가 CharProcs로 내장 → 임베딩 취급 pass."""
    path = tmp_path / "type3.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    page = pdf.pages[0]
    glyph = pdf.make_stream(b"500 0 d0")  # 폭만 지정하는 빈 글리프
    t3 = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/Type3"),
            FontBBox=[0, 0, 1000, 1000],
            FontMatrix=[0.001, 0, 0, 0.001, 0, 0],
            CharProcs=pikepdf.Dictionary(a=glyph),
            Encoding=pikepdf.Dictionary(
                Type=pikepdf.Name("/Encoding"),
                Differences=[97, pikepdf.Name("/a")],
            ),
            FirstChar=97,
            LastChar=97,
            Widths=[500],
        )
    )
    page["/Resources"] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=t3))
    page.Contents = pdf.make_stream(b"BT /F1 12 Tf 40 100 Td (a) Tj ET")
    pdf.save(path)

    r = _run(path)
    assert r.status == CheckStatus.PASS, r.detail
    used = [x for x in r.measured["fonts"] if x["used"]]
    assert used and used[0]["embedded"] is True


def test_unresolvable_font_key_is_uncertain(tmp_path):
    """콘텐츠가 리소스에 없는 폰트 키를 참조 → 판정 불가 uncertain (fail 아님)."""
    path = tmp_path / "missing_res.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    page = pdf.pages[0]
    page.Contents = pdf.make_stream(b"BT /F9 12 Tf 40 100 Td (x) Tj ET")
    pdf.save(path)

    r = _run(path)
    assert r.status == CheckStatus.UNCERTAIN, r.detail
    assert r.pages == [0]
    unknown = [x for x in r.measured["fonts"] if x["embedded"] is None]
    assert unknown and unknown[0]["used"] is True
