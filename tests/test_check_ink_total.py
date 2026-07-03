"""ink_total 체크 테스트.

- 코퍼스 실파일 스팟: ink_total 결함 파일(360% 사각형)에서 warn 검출,
  정상 파일 5종에서 침묵(pass).
- 임계 근처: tmp_path에 pikepdf로 직접 fixture를 만들어 300 / 300.4 / 301% 경계 확인.
- 이벤트 종류(fill·stroke·text)와 비CMYK 무시, 다중 페이지 pages 기록 확인.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from core.preflight.checks.ink_total import check_ink_total
from core.preflight.engine import CheckContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS_INK = sorted((ROOT / "data" / "samples" / "corpus").glob("*ink_total*.pdf"))
CLEAN = sorted((ROOT / "data" / "samples" / "clean").glob("*.pdf"))


def _run(path: Path):
    """체크 1개만 직접 실행 (다른 체크 모듈 임포트 없이 — 병렬 작업 격리)."""
    ctx = CheckContext(path)
    try:
        return check_ink_total(ctx)
    finally:
        ctx.close()


def _make_pdf(path: Path, page_streams: list[str], size=(200, 200)) -> Path:
    """페이지별 콘텐츠 스트림 문자열로 최소 PDF 생성."""
    pdf = pikepdf.new()
    for s in page_streams:
        page = pdf.add_blank_page(page_size=size)
        page.Contents = pdf.make_stream(s.encode("ascii"))
    pdf.save(path)
    return path


def _rect_fill(c: float, m: float, y: float, k: float) -> str:
    return f"{c} {m} {y} {k} k 10 10 100 100 re f\n"


# ---------------------------------------------------------------- 코퍼스 스팟


def test_corpus_has_ink_total_files():
    assert len(CORPUS_INK) >= 1, "코퍼스에 ink_total 결함 파일이 있어야 한다"
    assert len(CLEAN) >= 2, "정상 샘플이 2개 이상 있어야 한다"


@pytest.mark.parametrize("path", CORPUS_INK, ids=lambda p: p.name)
def test_corpus_ink_defect_detected(path):
    """주입기 결함: 0.9 0.9 0.9 0.9 k 사각형(360%) → warn + 실측 350~400%."""
    r = _run(path)
    assert r.status == CheckStatus.WARN, path.name
    assert 350.0 <= r.measured["max_ink_percent"] <= 400.0, r.measured
    assert r.measured["over_objects"], "초과 객체가 기록되어야 한다"
    assert all(o["percent"] > 300.5 for o in r.measured["over_objects"])
    assert r.pages, "문제 페이지가 기록되어야 한다"
    assert r.required == {"max_percent": 300}
    assert not r.autofix.available
    assert "잉크 리미팅" in r.autofix.note


@pytest.mark.parametrize("path", CLEAN, ids=lambda p: p.name)
def test_clean_files_pass(path):
    """정상 파일: 배경≤30%·밴드≤130%·K100 텍스트뿐 → pass, 침묵."""
    r = _run(path)
    assert r.status == CheckStatus.PASS, (path.name, r.measured)
    assert r.measured["max_ink_percent"] <= 300.0
    assert r.measured["over_objects"] == []
    assert r.pages == []
    # 프로토타입 한계 고지: 이미지 픽셀 잉크량 제외
    assert "이미지 제외" in r.detail


# ---------------------------------------------------------------- 임계 근처 fixture


def test_exact_300_passes(tmp_path):
    """정확히 300%는 통과."""
    p = _make_pdf(tmp_path / "ink300.pdf", [_rect_fill(0.75, 0.75, 0.75, 0.75)])
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert r.measured["max_ink_percent"] == pytest.approx(300.0, abs=0.05)
    assert r.pages == []


def test_within_tolerance_passes(tmp_path):
    """300.4% — +0.5%p 허용 오차 안이므로 통과."""
    p = _make_pdf(tmp_path / "ink300_4.pdf", [_rect_fill(0.751, 0.751, 0.751, 0.751)])
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert r.measured["over_objects"] == []


def test_just_over_tolerance_warns(tmp_path):
    """301% — 허용 오차(300.5) 초과이므로 warn."""
    p = _make_pdf(tmp_path / "ink301.pdf", [_rect_fill(0.7525, 0.7525, 0.7525, 0.7525)])
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["max_ink_percent"] == pytest.approx(301.0, abs=0.05)
    assert r.measured["over_objects"][0]["kind"] == "fill"
    assert r.pages == [0]


def test_stroke_ink_counted(tmp_path):
    """스트로크 색(K 연산자)도 잉크 합에 포함 — kind='stroke'."""
    p = _make_pdf(
        tmp_path / "stroke.pdf",
        ["0.9 0.9 0.9 0.9 K 2 w 10 10 m 150 150 l S\n"],
    )
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["over_objects"][0]["kind"] == "stroke"
    assert r.measured["max_ink_percent"] == pytest.approx(360.0, abs=0.05)


def test_text_ink_counted(tmp_path):
    """텍스트 fill 색도 잉크 합에 포함 — kind='text'."""
    p = _make_pdf(
        tmp_path / "text.pdf",
        ["BT /F1 12 Tf 0.8 0.8 0.8 0.8 k 20 20 Td (Hi) Tj ET\n"],
    )
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["over_objects"][0]["kind"] == "text"
    assert r.measured["max_ink_percent"] == pytest.approx(320.0, abs=0.05)


def test_non_cmyk_ignored(tmp_path):
    """RGB·Gray 채움은 잉크 합 산출 대상이 아니다 → 측정값 0, pass."""
    p = _make_pdf(
        tmp_path / "rgb.pdf",
        ["1 0 0 rg 10 10 100 100 re f\n0 g 20 20 50 50 re f\n"],
    )
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert r.measured["max_ink_percent"] == 0.0
    assert r.measured["over_objects"] == []


def test_multipage_reports_bad_page_only(tmp_path):
    """2페이지 중 2번째만 초과 → pages == [1], 최대값은 초과 페이지 값."""
    p = _make_pdf(
        tmp_path / "multi.pdf",
        [_rect_fill(0.1, 0.1, 0.1, 0.0), _rect_fill(0.9, 0.9, 0.9, 0.9)],
    )
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.pages == [1]
    assert r.measured["max_ink_percent"] == pytest.approx(360.0, abs=0.05)
    assert all(o["page"] == 1 for o in r.measured["over_objects"])
