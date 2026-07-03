"""black_type 체크 테스트 — 검정 텍스트 먹1도(K only) 검사.

1) 코퍼스 스팟: black_type 결함 파일에서 warn 검출, 정상 파일에서 pass
2) 임계 근처: pikepdf로 직접 만든 최소 fixture (K≥0.9 / C+M+Y 0.05 경계)
3) 무시 규칙: render_mode 3, DeviceGray, 비검정 CMYK
4) 예외 격리: 깨진 파일 → uncertain (예외가 밖으로 나가지 않는다)
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from core.preflight.checks.black_type import check_black_type
from core.preflight.engine import CheckContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "samples" / "corpus"

# 코퍼스 파일명에 결함 id가 들어있다
DEFECT_FILES = sorted(CORPUS.glob("*black_type*.pdf"))
CLEAN_FILES = sorted(CORPUS.glob("clean_*.pdf"))


def _run(path: Path):
    """체크 1회 실행 (컨텍스트 정리 포함)."""
    ctx = CheckContext(path)
    try:
        return check_black_type(ctx)
    finally:
        ctx.close()


def _make_pdf(tmp_path: Path, name: str, contents: list[bytes]) -> Path:
    """페이지별 콘텐츠 스트림으로 최소 PDF 생성 (폰트 리소스는 워커가 참조하지 않음)."""
    pdf = pikepdf.new()
    for content in contents:
        page = pdf.add_blank_page(page_size=(200, 200))
        page.obj["/Contents"] = pdf.make_stream(content)
    out = tmp_path / name
    pdf.save(out)
    return out


# ---------------------------------------------------------------- 코퍼스 스팟


@pytest.mark.parametrize("path", DEFECT_FILES, ids=lambda p: p.name)
def test_corpus_defect_files_warn(path: Path):
    """black_type 결함 파일: warn + 주입된 리치블랙(0.6 0.5 0.4 1) 실측."""
    r = _run(path)
    assert r.status == CheckStatus.WARN, path.name
    texts = r.measured["rich_black_texts"]
    assert texts, "위반 목록이 비어 있으면 안 됨"
    # 주입기는 본문을 0.6 0.5 0.4 1 k 로 만든다 (manifest params)
    assert any(t["cmyk"] == [0.6, 0.5, 0.4, 1.0] for t in texts), texts
    assert r.pages, "위반 페이지가 기록돼야 함"
    assert r.required == {"black_text": "0 0 0 1 k"}
    assert r.autofix.available is False


def test_corpus_defect_files_exist():
    """glob이 결함 파일을 최소 1개는 찾아야 스팟 테스트가 유효하다."""
    assert len(DEFECT_FILES) >= 1
    assert len(CLEAN_FILES) >= 2


@pytest.mark.parametrize("path", CLEAN_FILES, ids=lambda p: p.name)
def test_corpus_clean_files_pass(path: Path):
    """정상 파일: 타이틀·본문 모두 K100 → pass, 위반 0건."""
    r = _run(path)
    assert r.status == CheckStatus.PASS, (path.name, r.measured)
    assert r.measured["rich_black_texts"] == []
    assert r.pages == []


def test_corpus_multipage_records_all_pages():
    """2페이지 결함 파일(multi_15): 위반 페이지가 전부 pages에 기록된다."""
    cands = sorted(CORPUS.glob("multi_15*black_type*.pdf"))
    if not cands:
        pytest.skip("multi_15 black_type 파일 없음")
    r = _run(cands[0])
    assert r.status == CheckStatus.WARN
    assert r.pages == [0, 1]


# ---------------------------------------------------------------- 임계 근처 fixture


def test_rich_black_text_warns(tmp_path):
    """전형적 리치블랙(0.6 0.5 0.4 1) 텍스트 → warn."""
    p = _make_pdf(
        tmp_path,
        "rich.pdf",
        [b"BT /F1 12 Tf 20 100 Td 0.6 0.5 0.4 1 k (Hello) Tj ET"],
    )
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["rich_black_texts"] == [{"page": 0, "cmyk": [0.6, 0.5, 0.4, 1.0]}]
    assert r.pages == [0]


def test_k100_text_passes(tmp_path):
    """K100 (0 0 0 1 k) → pass."""
    p = _make_pdf(tmp_path, "k100.pdf", [b"BT /F1 12 Tf 20 100 Td 0 0 0 1 k (Hi) Tj ET"])
    r = _run(p)
    assert r.status == CheckStatus.PASS
    assert r.measured["rich_black_texts"] == []


def test_cmy_sum_at_threshold_passes(tmp_path):
    """C+M+Y = 0.05 정확히 (경계값) → 위반 아님 (> 0.05 만 위반)."""
    p = _make_pdf(
        tmp_path,
        "edge_pass.pdf",
        [b"BT /F1 12 Tf 20 100 Td 0.02 0.02 0.01 0.9 k (Hi) Tj ET"],
    )
    r = _run(p)
    assert r.status == CheckStatus.PASS


def test_cmy_sum_just_over_threshold_warns(tmp_path):
    """K=0.9 (검정 의도 경계) + C+M+Y = 0.06 → warn."""
    p = _make_pdf(
        tmp_path,
        "edge_warn.pdf",
        [b"BT /F1 12 Tf 20 100 Td 0.02 0.02 0.02 0.9 k (Hi) Tj ET"],
    )
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.measured["rich_black_texts"][0]["cmyk"] == [0.02, 0.02, 0.02, 0.9]


def test_dark_color_below_k_intent_passes(tmp_path):
    """K=0.89 — 검정 '의도'(K≥0.9) 미달인 진한 색 → 위반 아님."""
    p = _make_pdf(
        tmp_path,
        "dark.pdf",
        [b"BT /F1 12 Tf 20 100 Td 0.6 0.5 0.4 0.89 k (Hi) Tj ET"],
    )
    r = _run(p)
    assert r.status == CheckStatus.PASS


def test_invisible_text_ignored(tmp_path):
    """render_mode 3(비표시) 리치블랙 텍스트 → 무시하고 pass."""
    p = _make_pdf(
        tmp_path,
        "invisible.pdf",
        [b"BT /F1 12 Tf 3 Tr 20 100 Td 0.6 0.5 0.4 1 k (Hi) Tj ET"],
    )
    r = _run(p)
    assert r.status == CheckStatus.PASS


def test_devicegray_text_passes(tmp_path):
    """DeviceGray 텍스트(0 g)는 먹1도 취급 → pass."""
    p = _make_pdf(tmp_path, "gray.pdf", [b"BT /F1 12 Tf 20 100 Td 0 g (Hi) Tj ET"])
    r = _run(p)
    assert r.status == CheckStatus.PASS


def test_multipage_only_bad_page_recorded(tmp_path):
    """페이지0 K100 정상, 페이지1 리치블랙 → pages == [1]."""
    p = _make_pdf(
        tmp_path,
        "two.pdf",
        [
            b"BT /F1 12 Tf 20 100 Td 0 0 0 1 k (ok) Tj ET",
            b"BT /F1 12 Tf 20 100 Td 0.1 0.1 0.1 0.95 k (bad) Tj ET",
        ],
    )
    r = _run(p)
    assert r.status == CheckStatus.WARN
    assert r.pages == [1]
    assert r.measured["rich_black_texts"] == [{"page": 1, "cmyk": [0.1, 0.1, 0.1, 0.95]}]


def test_rich_black_vector_fill_not_text_passes(tmp_path):
    """텍스트가 아닌 벡터 채움의 리치블랙은 이 체크 대상 아님 → pass."""
    p = _make_pdf(
        tmp_path,
        "fill.pdf",
        [b"0.6 0.5 0.4 1 k 10 10 50 50 re f"],
    )
    r = _run(p)
    assert r.status == CheckStatus.PASS


# ---------------------------------------------------------------- 예외 격리


def test_broken_file_returns_uncertain(tmp_path):
    """PDF가 아닌 파일 → 예외 대신 uncertain 반환."""
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"this is not a pdf at all")
    r = _run(p)
    assert r.status == CheckStatus.UNCERTAIN
