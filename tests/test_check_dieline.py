"""dieline 체크 테스트.

- 코퍼스 실파일 스팟: dieline 결함 파일에서 uncertain 검출, 정상 파일에서 pass 침묵.
- tmp_path 합성 fixture (pikepdf 직접 생성): 칼선 이름 변형, 비칼선 별색, Form XObject
  내부에만 칼선이 있는 경우, 콘텐츠 없는 페이지 등 경계 케이스.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

# 내 모듈만 직접 임포트 — 데코레이터가 레지스트리에 등록하고 원함수를 그대로 반환한다.
from core.preflight.checks.dieline import check_dieline
from core.preflight.engine import CheckContext, OrderContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "samples" / "corpus"

PRODUCTS = ("sticker", "label", "namecard", "flyer", "poster")


def run_check(pdf_path: Path, product: str | None = None):
    """CheckContext를 만들어 dieline 체크 1회 실행 (닫기 보장)."""
    ctx = CheckContext(pdf_path, OrderContext(product=product))
    try:
        return check_dieline(ctx)
    finally:
        ctx.close()


def product_from_name(path: Path) -> str:
    """코퍼스 파일명에 상품명이 들어있다 (예: single_20_sticker_dieline.pdf)."""
    for prod in PRODUCTS:
        if prod in path.name:
            return prod
    raise AssertionError(f"파일명에서 상품을 찾을 수 없음: {path.name}")


def corpus_glob(pattern: str) -> list[Path]:
    files = sorted(CORPUS.glob(pattern))
    assert files, f"코퍼스 파일 없음: {pattern}"
    return files


# ---------------------------------------------------------------- 등록


def test_registered_in_registry():
    """@register_check("dieline") 등록 확인 (다른 체크 모듈은 임포트하지 않는다)."""
    from core.preflight.engine import _REGISTRY

    assert "dieline" in _REGISTRY


# ---------------------------------------------------------------- 코퍼스 스팟


def test_corpus_dieline_defect_files_uncertain():
    """dieline 결함 파일(파일명에 'dieline'): 칼선 상품인데 별색 자체가 없다 → uncertain."""
    for f in corpus_glob("*dieline*.pdf"):
        r = run_check(f, product=product_from_name(f))
        assert r.status == CheckStatus.UNCERTAIN, f"{f.name}: {r.status} / {r.detail}"
        assert r.measured["dieline_present"] is False, f.name
        # 주입기 보장: dieline 결함 파일엔 Separation 자체가 없다
        assert r.measured["spot_names"] == [], f.name
        assert r.pages, f"{f.name}: 문제 페이지 기록 없음"


def test_corpus_clean_dieline_products_pass():
    """정상 sticker/label: Separation:CutContour 칼선 존재 → pass + dieline_present=True."""
    files = corpus_glob("clean_*sticker*.pdf") + corpus_glob("clean_*label*.pdf")
    assert len(files) >= 2
    for f in files:
        r = run_check(f, product=product_from_name(f))
        assert r.status == CheckStatus.PASS, f"{f.name}: {r.status} / {r.detail}"
        assert r.measured["dieline_present"] is True, f.name
        assert "CutContour" in r.measured["spot_names"], f.name
        assert r.pages == [], f.name


def test_corpus_clean_other_products_pass():
    """정상 namecard/flyer/poster: 별색 없음 → pass (침묵)."""
    files = (
        corpus_glob("clean_*namecard*.pdf")
        + corpus_glob("clean_*flyer*.pdf")
        + corpus_glob("clean_*poster*.pdf")
    )
    assert len(files) >= 2
    for f in files:
        r = run_check(f, product=product_from_name(f))
        assert r.status == CheckStatus.PASS, f"{f.name}: {r.status} / {r.detail}"
        assert r.measured["dieline_present"] is False, f.name
        assert r.measured["spot_names"] == [], f.name


def test_corpus_product_mismatch_uncertain():
    """칼선이 있는 파일을 비칼선 상품 주문으로 검사 → 의도 확인 uncertain."""
    f = corpus_glob("clean_*sticker*.pdf")[0]
    r = run_check(f, product="flyer")
    assert r.status == CheckStatus.UNCERTAIN
    assert r.measured["dieline_present"] is True
    assert r.pages == [0]


def test_corpus_product_none_pass_records_presence():
    """주문 정보 없음(product=None) → 항상 pass, 존재 여부만 measured에 기록."""
    with_dieline = corpus_glob("clean_*sticker*.pdf")[0]
    without_dieline = corpus_glob("single_*dieline*.pdf")[0]

    r1 = run_check(with_dieline, product=None)
    assert r1.status == CheckStatus.PASS
    assert r1.measured["dieline_present"] is True

    r2 = run_check(without_dieline, product=None)
    assert r2.status == CheckStatus.PASS
    assert r2.measured["dieline_present"] is False


# ---------------------------------------------------------------- 합성 fixture


def _tint_fn(pdf: pikepdf.Pdf):
    """Separation의 틴트 변환 함수 (Type2, DeviceCMYK 대상)."""
    return pdf.make_indirect(
        pikepdf.Dictionary(
            FunctionType=2,
            Domain=[0, 1],
            C0=[0, 0, 0, 0],
            C1=[0, 1, 0, 0],
            N=1,
        )
    )


def _sep_array(pdf: pikepdf.Pdf, sep_name: str) -> pikepdf.Array:
    return pikepdf.Array(
        [
            pikepdf.Name("/Separation"),
            pikepdf.Name("/" + sep_name),
            pikepdf.Name("/DeviceCMYK"),
            _tint_fn(pdf),
        ]
    )


def make_pdf_with_sep(path: Path, sep_name: str) -> Path:
    """페이지 리소스 /ColorSpace에 Separation을 정의하고 스트로크 1개를 긋는 최소 PDF."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.obj["/Resources"] = pikepdf.Dictionary(
        ColorSpace=pikepdf.Dictionary(CS0=_sep_array(pdf, sep_name))
    )
    page.obj["/Contents"] = pikepdf.Stream(
        pdf, b"q /CS0 CS 1 SCN 1 w 20 20 m 180 180 l S Q\n"
    )
    pdf.save(path)
    return path


def make_pdf_form_only_dieline(path: Path) -> Path:
    """칼선이 Form XObject 내부 리소스에만 정의된 PDF (페이지 리소스엔 /ColorSpace 없음)."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    form = pikepdf.Stream(
        pdf,
        b"/CS0 CS 1 SCN 1 w 10 10 m 190 190 l S\n",
        Type=pikepdf.Name("/XObject"),
        Subtype=pikepdf.Name("/Form"),
        BBox=pikepdf.Array([0, 0, 200, 200]),
        Resources=pikepdf.Dictionary(
            ColorSpace=pikepdf.Dictionary(CS0=_sep_array(pdf, "CutContour"))
        ),
    )
    page.obj["/Resources"] = pikepdf.Dictionary(
        XObject=pikepdf.Dictionary(Fm0=pdf.make_indirect(form))
    )
    page.obj["/Contents"] = pikepdf.Stream(pdf, b"q /Fm0 Do Q\n")
    pdf.save(path)
    return path


def make_blank_pdf(path: Path) -> Path:
    """리소스·콘텐츠 없는 빈 페이지 1장."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(path)
    return path


@pytest.mark.parametrize(
    "sep_name",
    ["CutContour", "cutcontour", "Thru-Cut", "KissCut", "DieLine", "CUT", "die"],
)
def test_dieline_name_variants_detected(tmp_path, sep_name):
    """칼선 이름 패턴(대소문자 무시) 변형이 전부 칼선으로 인식된다 → sticker에서 pass."""
    f = make_pdf_with_sep(tmp_path / f"sep_{sep_name.replace('-', '_')}.pdf", sep_name)
    r = run_check(f, product="sticker")
    assert r.status == CheckStatus.PASS, f"{sep_name}: {r.status} / {r.detail}"
    assert r.measured["dieline_present"] is True
    assert sep_name in r.measured["spot_names"]


@pytest.mark.parametrize("sep_name", ["Gold", "PANTONE185C", "White"])
def test_non_dieline_spot_not_matched(tmp_path, sep_name):
    """칼선 이름이 아닌 별색은 dieline으로 오인하지 않는다."""
    f = make_pdf_with_sep(tmp_path / f"spot_{sep_name}.pdf", sep_name)

    # 칼선 상품: 별색은 있지만 칼선은 아님 → 칼선 없음 uncertain
    r = run_check(f, product="sticker")
    assert r.status == CheckStatus.UNCERTAIN, f"{sep_name}: {r.status}"
    assert r.measured["dieline_present"] is False
    assert sep_name in r.measured["spot_names"]

    # 비칼선 상품: 칼선 아님 → pass
    r2 = run_check(f, product="flyer")
    assert r2.status == CheckStatus.PASS
    assert r2.measured["dieline_present"] is False


def test_dieline_only_in_form_xobject(tmp_path):
    """칼선이 Form XObject 안에만 있어도 검출된다 (이벤트 경로 + 리소스 재귀)."""
    f = make_pdf_form_only_dieline(tmp_path / "form_dieline.pdf")
    r = run_check(f, product="label")
    assert r.status == CheckStatus.PASS, r.detail
    assert r.measured["dieline_present"] is True
    assert "CutContour" in r.measured["spot_names"]


def test_blank_page_no_crash(tmp_path):
    """리소스·콘텐츠 없는 페이지에서도 예외 없이 판정한다."""
    f = make_blank_pdf(tmp_path / "blank.pdf")

    r = run_check(f, product="sticker")  # 칼선 상품인데 아무것도 없음 → uncertain
    assert r.status == CheckStatus.UNCERTAIN
    assert r.measured["dieline_present"] is False
    assert r.pages == [0]

    r2 = run_check(f, product=None)  # 주문 미상 → pass
    assert r2.status == CheckStatus.PASS


def test_multipage_pages_recorded(tmp_path):
    """다중 페이지: 칼선이 일부 페이지에만 있어도 present=True, 비칼선 상품이면 해당 페이지 기록."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))  # p0: 빈 페이지
    p1 = pdf.add_blank_page(page_size=(200, 200))  # p1: 칼선
    p1.obj["/Resources"] = pikepdf.Dictionary(
        ColorSpace=pikepdf.Dictionary(CS0=_sep_array(pdf, "CutContour"))
    )
    p1.obj["/Contents"] = pikepdf.Stream(pdf, b"q /CS0 CS 1 SCN 1 w 20 20 m 180 180 l S Q\n")
    f = tmp_path / "two_pages.pdf"
    pdf.save(f)

    r = run_check(f, product="namecard")
    assert r.status == CheckStatus.UNCERTAIN
    assert r.measured["dieline_present"] is True
    assert r.pages == [1]

    r2 = run_check(f, product="sticker")
    assert r2.status == CheckStatus.PASS
    assert r2.measured["dieline_present"] is True
