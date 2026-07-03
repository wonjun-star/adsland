"""page_count 체크 테스트.

- 코퍼스 실파일 스팟: 결함 파일(파일 2p vs 주문 1p) 검출 + 정상 파일 pass
- 경계/특수 케이스: tmp_path에 pikepdf로 직접 fixture 생성
  (일치, 초과, 부족, 주문 미지정, 깨진 파일)
"""

from __future__ import annotations

import json
from pathlib import Path

import pikepdf

from core.preflight.checks.page_count import check_page_count
from core.preflight.engine import CheckContext, OrderContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "samples" / "corpus"
MANIFEST = ROOT / "data" / "samples" / "manifest.json"


def _order_from_manifest(pdf_path: Path) -> OrderContext:
    """manifest.json에서 해당 파일의 주문 정보를 찾아 OrderContext로 변환."""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for entry in data["files"]:
        if Path(entry["file"]).name == pdf_path.name:
            o = entry.get("order") or {}
            size = o.get("size_mm")
            return OrderContext(
                product=entry.get("product"),
                size_mm=tuple(size) if size else None,
                page_count=o.get("page_count"),
            )
    raise AssertionError(f"manifest에 없는 파일: {pdf_path.name}")


def _run(pdf_path: Path, order: OrderContext | None):
    ctx = CheckContext(pdf_path, order)
    try:
        return check_page_count(ctx)
    finally:
        ctx.close()


def _make_pdf(path: Path, n_pages: int) -> Path:
    """빈 페이지 n장짜리 PDF fixture (내용은 페이지 수 검사와 무관)."""
    pdf = pikepdf.new()
    for _ in range(n_pages):
        pdf.add_blank_page(page_size=(255.12, 141.73))  # 약 90×50mm
    pdf.save(path)
    return path


# ---------------------------------------------------------------- 코퍼스 스팟


def test_corpus_defect_files_fail():
    """page_count 결함 코퍼스(파일명에 id 포함): 파일 2p vs 주문 1p → fail."""
    files = sorted(CORPUS.glob("*page_count*.pdf"))
    assert files, "page_count 결함 코퍼스 파일을 찾지 못함"
    for f in files:
        r = _run(f, _order_from_manifest(f))
        assert r.status == CheckStatus.FAIL, f.name
        assert r.measured["file_pages"] == 2, f.name
        assert r.measured["order_pages"] == 1, f.name
        assert r.required == {"page_count": 1}, f.name
        assert not r.autofix.available, f.name  # 질문 대상 — autofix 불가
        assert r.pages == [1], f.name  # 초과분(2번째 페이지, 0-base)


def test_corpus_clean_files_pass():
    """정상 코퍼스 파일(주문 1p, 파일 1p)에서는 침묵해야 한다."""
    files = sorted(CORPUS.glob("clean_*.pdf"))
    assert len(files) >= 2, "정상 코퍼스 파일 부족"
    for f in files:
        r = _run(f, _order_from_manifest(f))
        assert r.status == CheckStatus.PASS, f.name
        assert r.measured["file_pages"] == r.measured["order_pages"], f.name


# ------------------------------------------------------------ 직접 fixture


def test_exact_match_passes(tmp_path):
    p = _make_pdf(tmp_path / "three.pdf", 3)
    r = _run(p, OrderContext(page_count=3))
    assert r.status == CheckStatus.PASS
    assert r.measured == {"file_pages": 3, "order_pages": 3}
    assert r.required == {"page_count": 3}
    assert r.pages == []


def test_order_none_passes_with_detail(tmp_path):
    """주문에 페이지 수가 없으면 비교 불가 → pass + '주문 미지정' 기록."""
    p = _make_pdf(tmp_path / "two.pdf", 2)
    r = _run(p, OrderContext())  # page_count=None
    assert r.status == CheckStatus.PASS
    assert "주문 미지정" in r.detail
    assert r.measured == {"file_pages": 2, "order_pages": None}


def test_file_more_than_order_fails_with_extra_pages(tmp_path):
    """파일이 주문보다 많으면 초과분 페이지 인덱스를 pages에 기록."""
    p = _make_pdf(tmp_path / "four.pdf", 4)
    r = _run(p, OrderContext(page_count=2))
    assert r.status == CheckStatus.FAIL
    assert r.measured == {"file_pages": 4, "order_pages": 2}
    assert r.pages == [2, 3]
    assert not r.autofix.available


def test_file_fewer_than_order_fails(tmp_path):
    """파일이 주문보다 적어도 fail (부족분은 특정 페이지로 지목 불가 → pages 비움)."""
    p = _make_pdf(tmp_path / "one.pdf", 1)
    r = _run(p, OrderContext(page_count=2))
    assert r.status == CheckStatus.FAIL
    assert r.measured == {"file_pages": 1, "order_pages": 2}
    assert r.pages == []


def test_broken_pdf_is_uncertain(tmp_path):
    """열 수 없는 파일이면 예외를 밖으로 내보내지 않고 uncertain."""
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4 this is not a real pdf")
    r = _run(p, OrderContext(page_count=1))
    assert r.status == CheckStatus.UNCERTAIN
    assert "측정 실패" in r.detail
