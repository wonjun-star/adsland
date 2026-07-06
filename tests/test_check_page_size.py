"""page_size 체크 테스트.

- 코퍼스 실파일 스팟: page_size 결함 파일에서 검출 + 정상 파일에서 침묵.
- 전수 스윕: 50종 전부 — page_size 결함 유무와 fail/pass가 1:1 대응.
- 임계 근처·회전·TrimBox 부재·페이지 크기 혼합 등은 pikepdf로 fixture 직접 생성.
"""

from __future__ import annotations

import json
from pathlib import Path

import pikepdf

from core.preflight.checks.page_size import TOLERANCE_MM, check_page_size
from core.preflight.engine import CheckContext, OrderContext, mm_to_pt
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "samples" / "corpus"
MANIFEST = ROOT / "data" / "samples" / "manifest.json"


def _manifest_by_name() -> dict[str, dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {Path(e["file"]).name: e for e in data["files"]}


def _run(pdf_path: Path, order: OrderContext | None = None):
    ctx = CheckContext(pdf_path, order)
    try:
        return check_page_size(ctx)
    finally:
        ctx.close()


def _make_pdf(path: Path, sizes_mm, with_trimbox: bool = True, bleed_mm: float = 3.0) -> Path:
    """재단 크기 목록으로 fixture PDF 생성. TrimBox 있으면 사방 bleed 인셋 구조."""
    pdf = pikepdf.new()
    for w_mm, h_mm in sizes_mm:
        tw, th = mm_to_pt(w_mm), mm_to_pt(h_mm)
        if with_trimbox:
            b = mm_to_pt(bleed_mm)
            page = pdf.add_blank_page(page_size=(tw + 2 * b, th + 2 * b))
            page.obj["/TrimBox"] = pikepdf.Array([b, b, b + tw, b + th])
        else:
            pdf.add_blank_page(page_size=(tw, th))
    pdf.save(path)
    return path


# ---------------------------------------------------------------- 코퍼스 스팟


def test_corpus_page_size_defect_files_fail():
    """page_size 결함 파일(파일명에 id 포함)은 전부 fail + 실측이 GT와 일치."""
    entries = _manifest_by_name()
    files = sorted(CORPUS.glob("*page_size*.pdf"))
    assert len(files) >= 1, "코퍼스에 page_size 결함 파일이 있어야 함"
    for f in files:
        e = entries[f.name]
        r = _run(f, OrderContext(size_mm=tuple(e["order"]["size_mm"])))
        assert r.status == CheckStatus.FAIL, f"{f.name}: {r.status} ({r.detail})"
        assert not r.autofix.available, "크기 불일치는 autofix 불가"
        assert r.pages, "문제 페이지가 기록되어야 함"
        assert r.required == {"tolerance_mm": TOLERANCE_MM}
        # 실측 크기 = manifest ground truth (주입기가 보장하는 물리값)
        d = next(x for x in e["defects"] if x["id"] == "page_size")
        want_w, want_h = d["params"]["file_size_mm"]
        got_w, got_h = r.measured["file_size_mm"]
        assert abs(got_w - want_w) < 0.1 and abs(got_h - want_h) < 0.1, f.name
        assert r.measured["order_size_mm"] == list(map(float, e["order"]["size_mm"]))


def test_corpus_clean_files_pass():
    """정상 파일(≥2개)에서는 침묵 (pass, rotated=False)."""
    entries = _manifest_by_name()
    files = sorted(CORPUS.glob("clean_*.pdf"))
    assert len(files) >= 2
    for f in files:
        e = entries[f.name]
        r = _run(f, OrderContext(size_mm=tuple(e["order"]["size_mm"])))
        assert r.status == CheckStatus.PASS, f"{f.name}: {r.status} ({r.detail})"
        assert r.measured["rotated"] is False
        assert r.pages == []


def test_corpus_full_sweep():
    """50종 전수: page_size 결함 유무 ↔ fail/pass 1:1 (다른 결함은 이 체크에 무관)."""
    entries = _manifest_by_name()
    files = sorted(CORPUS.glob("*.pdf"))
    assert len(files) == 50
    for f in files:
        e = entries[f.name]
        r = _run(f, OrderContext(size_mm=tuple(e["order"]["size_mm"])))
        has_defect = any(d["id"] == "page_size" for d in e["defects"])
        want = CheckStatus.FAIL if has_defect else CheckStatus.PASS
        assert r.status == want, f"{f.name}: {r.status} ({r.detail})"


# ---------------------------------------------------------------- 임계 근처 fixture


def test_within_tolerance_passes(tmp_path):
    """오차 0.49mm ≤ 0.5mm → pass."""
    p = _make_pdf(tmp_path / "near.pdf", [(90.49, 50.0)])
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.PASS, r.detail


def test_undersized_fails(tmp_path):
    """규격보다 작으면(재단 후 흰 여백) fail — 이건 진짜 문제다."""
    p = _make_pdf(tmp_path / "small.pdf", [(85.0, 50.0)])
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.FAIL
    assert r.pages == [0]
    assert r.measured["file_size_mm"] == [85.0, 50.0]


def test_much_larger_fails(tmp_path):
    """재단여백으로 볼 수 없을 만큼 크면 fail (규격 + 8mm 초과)."""
    p = _make_pdf(tmp_path / "big.pdf", [(100.0, 60.0)])
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.FAIL
    assert r.pages == [0]


def test_larger_by_bleed_passes(tmp_path):
    """규격 + 재단여백(≈3mm/변)만큼 큰 건 정상 인쇄 파일 → pass, includes_bleed 표시."""
    p = _make_pdf(tmp_path / "bleed.pdf", [(96.0, 56.0)])
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.PASS, r.detail
    assert r.measured.get("includes_bleed") is True


def test_rotated_match_passes_with_flag(tmp_path):
    """가로세로 교환 일치 → pass + measured['rotated']=True."""
    p = _make_pdf(tmp_path / "rot.pdf", [(50.0, 90.0)])
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.PASS, r.detail
    assert r.measured["rotated"] is True


def test_no_order_size_skips_comparison(tmp_path):
    """주문 규격 미지정 → 비교 생략 pass, detail에 명시."""
    p = _make_pdf(tmp_path / "noorder.pdf", [(90.0, 50.0)])
    r = _run(p, OrderContext())
    assert r.status == CheckStatus.PASS
    assert "주문 규격 미지정" in r.detail
    assert r.measured["order_size_mm"] is None


def test_mediabox_fallback_noted(tmp_path):
    """TrimBox 없는 파일 → MediaBox 크기로 판정 + detail에 폴백 명시."""
    p = _make_pdf(tmp_path / "notrim.pdf", [(90.0, 50.0)], with_trimbox=False)
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.PASS, r.detail
    assert "MediaBox" in r.detail


def test_mixed_page_sizes_fail(tmp_path):
    """페이지 간 크기가 다르면 fail — 다른 크기의 페이지가 pages에 기록."""
    p = _make_pdf(tmp_path / "mixed.pdf", [(90.0, 50.0), (100.0, 50.0)])
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.FAIL
    assert 1 in r.pages
    assert r.measured["page_sizes_mm"] == [[90.0, 50.0], [100.0, 50.0]]


def test_mixed_page_sizes_fail_even_without_order(tmp_path):
    """주문 규격이 없어도 페이지 간 크기 불일치는 파일 자체 결함 → fail."""
    p = _make_pdf(tmp_path / "mixed2.pdf", [(90.0, 50.0), (100.0, 50.0)])
    r = _run(p, OrderContext())
    assert r.status == CheckStatus.FAIL
    assert r.pages == [1]


def test_rotated_pages_are_consistent(tmp_path):
    """서로 회전 관계인 2페이지는 동일 크기로 간주 — 주문과도 (교환 포함) 일치 → pass."""
    p = _make_pdf(tmp_path / "rotpages.pdf", [(90.0, 50.0), (50.0, 90.0)])
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.PASS, r.detail


def test_broken_file_returns_uncertain(tmp_path):
    """PDF가 아닌 파일 → 예외 대신 uncertain."""
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not a pdf at all")
    r = _run(p, OrderContext(size_mm=(90.0, 50.0)))
    assert r.status == CheckStatus.UNCERTAIN
    assert "측정 실패" in r.detail
