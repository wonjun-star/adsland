"""transparency 체크 테스트.

- 코퍼스 스팟: *transparency* 결함 파일에서 warn + /ca 0.5 검출,
  리셋용 /ca 1.0 (gRLs1) 은 목록에 없어야 한다. 정상 파일은 pass.
- 경계 fixture (tmp_path, pikepdf 직접 생성):
  /ca 1.0 만 있는 파일 → pass (존재만으로 판정 금지)
  /CA < 1, /BM Multiply, ExtGState /SMask 딕셔너리, 이미지 /SMask → warn
  /SMask /None, /BM /Normal·/Compatible → pass
  Form XObject 내부 ExtGState → 재귀 검출
  2페이지 중 2쪽만 결함 → pages=[1]
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from core.preflight.checks.transparency import check_transparency
from core.preflight.engine import CheckContext
from core.preflight.report import CheckStatus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "samples" / "corpus"
CLEAN = ROOT / "data" / "samples" / "clean"


def _run(pdf_path: Path):
    ctx = CheckContext(pdf_path)
    try:
        return check_transparency(ctx)
    finally:
        ctx.close()


# ---------------------------------------------------------------- 코퍼스 스팟

def _transparency_corpus_files() -> list[Path]:
    return sorted(CORPUS.glob("*transparency*.pdf"))


@pytest.mark.parametrize(
    "pdf_path", _transparency_corpus_files(), ids=lambda p: p.name
)
def test_corpus_defect_detected(pdf_path: Path):
    """결함 파일 전부에서 warn + ca 0.5 항목 검출."""
    r = _run(pdf_path)
    assert r.status == CheckStatus.WARN
    states = r.measured["transparent_states"]
    assert len(states) >= 1
    # 주입기는 /ca 0.5 를 넣는다
    assert any(abs(s.get("ca", 1.0) - 0.5) < 1e-6 for s in states)
    # 리셋용 /ca 1.0 (gRLs1) 은 투명 목록에 들어가면 안 된다
    for s in states:
        assert "ca" not in s or s["ca"] < 1.0
    assert r.pages  # 문제 페이지 기록


def test_corpus_has_defect_files():
    """glob이 빈 목록이면 스팟 테스트가 통째로 스킵되므로 방어."""
    assert len(_transparency_corpus_files()) >= 1


@pytest.mark.parametrize(
    "pdf_path",
    sorted(CLEAN.glob("*.pdf")) + sorted(CORPUS.glob("clean_*.pdf"))[:2],
    ids=lambda p: p.name,
)
def test_clean_files_pass(pdf_path: Path):
    """정상 파일에서는 침묵 (pass, 검출 0건)."""
    r = _run(pdf_path)
    assert r.status == CheckStatus.PASS
    assert r.measured["transparent_states"] == []
    assert r.pages == []


# ---------------------------------------------------------------- fixture 헬퍼

def _new_pdf(n_pages: int = 1) -> pikepdf.Pdf:
    pdf = pikepdf.new()
    for _ in range(n_pages):
        pdf.add_blank_page(page_size=(200, 200))
    return pdf


def _set_extgstate(pdf: pikepdf.Pdf, page_i: int, states: dict) -> None:
    """states: {"/g0": {"/ca": 0.5}, ...}"""
    page = pdf.pages[page_i]
    res = page.obj.get("/Resources", None)
    if res is None:
        res = pikepdf.Dictionary()
        page.obj["/Resources"] = res
    egs = pikepdf.Dictionary()
    for name, d in states.items():
        egs[name] = pikepdf.Dictionary(d)
    res["/ExtGState"] = egs


def _save(pdf: pikepdf.Pdf, path: Path) -> Path:
    pdf.save(path)
    pdf.close()
    return path


# ---------------------------------------------------------------- 경계 케이스

def test_reset_ca_one_only_passes(tmp_path: Path):
    """리셋용 /ca 1.0, /CA 1.0 만 있으면 pass — '존재'만으로 판정 금지."""
    pdf = _new_pdf()
    _set_extgstate(pdf, 0, {"/g0": {"/ca": 1.0, "/CA": 1.0}})
    r = _run(_save(pdf, tmp_path / "reset_only.pdf"))
    assert r.status == CheckStatus.PASS
    assert r.measured["transparent_states"] == []


def test_ca_below_one_warns(tmp_path: Path):
    """/ca 0.5 + 리셋 /ca 1.0 공존 → 0.5 만 잡는다 (코퍼스와 동일 구조)."""
    pdf = _new_pdf()
    _set_extgstate(pdf, 0, {"/g0": {"/ca": 0.5}, "/g1": {"/ca": 1.0}})
    r = _run(_save(pdf, tmp_path / "ca_half.pdf"))
    assert r.status == CheckStatus.WARN
    states = r.measured["transparent_states"]
    assert len(states) == 1
    assert states[0]["name"] == "g0"
    assert abs(states[0]["ca"] - 0.5) < 1e-6


def test_stroke_alpha_CA_warns(tmp_path: Path):
    """/CA(스트로크 알파) < 1 도 투명."""
    pdf = _new_pdf()
    _set_extgstate(pdf, 0, {"/g0": {"/CA": 0.7}})
    r = _run(_save(pdf, tmp_path / "CA.pdf"))
    assert r.status == CheckStatus.WARN
    assert abs(r.measured["transparent_states"][0]["CA"] - 0.7) < 1e-6


def test_blend_mode_multiply_warns(tmp_path: Path):
    """/BM /Multiply → warn. /Normal·/Compatible 은 pass."""
    pdf = _new_pdf()
    _set_extgstate(pdf, 0, {"/g0": {"/BM": pikepdf.Name("/Multiply")}})
    r = _run(_save(pdf, tmp_path / "bm_multiply.pdf"))
    assert r.status == CheckStatus.WARN
    assert r.measured["transparent_states"][0]["bm"] == "Multiply"

    pdf2 = _new_pdf()
    _set_extgstate(pdf2, 0, {
        "/g0": {"/BM": pikepdf.Name("/Normal")},
        "/g1": {"/BM": pikepdf.Name("/Compatible")},
    })
    r2 = _run(_save(pdf2, tmp_path / "bm_normal.pdf"))
    assert r2.status == CheckStatus.PASS


def test_extgstate_smask_dict_warns_none_passes(tmp_path: Path):
    """ExtGState /SMask 딕셔너리 → warn, /SMask /None → pass."""
    pdf = _new_pdf()
    smask_dict = pikepdf.Dictionary({"/S": pikepdf.Name("/Alpha")})
    _set_extgstate(pdf, 0, {"/g0": {"/SMask": smask_dict}})
    r = _run(_save(pdf, tmp_path / "smask_dict.pdf"))
    assert r.status == CheckStatus.WARN
    assert r.measured["transparent_states"][0]["smask"] is True

    pdf2 = _new_pdf()
    _set_extgstate(pdf2, 0, {"/g0": {"/SMask": pikepdf.Name("/None")}})
    r2 = _run(_save(pdf2, tmp_path / "smask_none.pdf"))
    assert r2.status == CheckStatus.PASS


def test_image_xobject_smask_warns(tmp_path: Path):
    """이미지 XObject 의 /SMask 스트림(알파 채널) 검출."""
    pdf = _new_pdf()
    smask = pdf.make_stream(
        b"\x80" * 4,
        Type=pikepdf.Name("/XObject"), Subtype=pikepdf.Name("/Image"),
        Width=2, Height=2,
        ColorSpace=pikepdf.Name("/DeviceGray"), BitsPerComponent=8,
    )
    img = pdf.make_stream(
        b"\xff" * 12,
        Type=pikepdf.Name("/XObject"), Subtype=pikepdf.Name("/Image"),
        Width=2, Height=2,
        ColorSpace=pikepdf.Name("/DeviceRGB"), BitsPerComponent=8,
        SMask=smask,
    )
    page = pdf.pages[0]
    res = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/Im0": img})})
    page.obj["/Resources"] = res
    r = _run(_save(pdf, tmp_path / "img_smask.pdf"))
    assert r.status == CheckStatus.WARN
    s = r.measured["transparent_states"][0]
    assert s["kind"] == "image_smask" and s["name"] == "Im0"


def test_form_xobject_nested_extgstate(tmp_path: Path):
    """Form XObject 내부 /Resources 의 투명 ExtGState 도 재귀로 잡는다."""
    pdf = _new_pdf()
    inner_res = pikepdf.Dictionary({
        "/ExtGState": pikepdf.Dictionary({"/gi": pikepdf.Dictionary({"/ca": 0.3})})
    })
    form = pdf.make_stream(
        b"",
        Type=pikepdf.Name("/XObject"), Subtype=pikepdf.Name("/Form"),
        BBox=pikepdf.Array([0, 0, 10, 10]), Resources=inner_res,
    )
    page = pdf.pages[0]
    page.obj["/Resources"] = pikepdf.Dictionary(
        {"/XObject": pikepdf.Dictionary({"/Fx0": form})}
    )
    r = _run(_save(pdf, tmp_path / "form_nested.pdf"))
    assert r.status == CheckStatus.WARN
    s = r.measured["transparent_states"][0]
    assert s["name"] == "gi" and abs(s["ca"] - 0.3) < 1e-6


def test_multipage_reports_defect_page_only(tmp_path: Path):
    """2페이지 중 2쪽(인덱스 1)만 투명 → pages=[1]."""
    pdf = _new_pdf(n_pages=2)
    _set_extgstate(pdf, 0, {"/g0": {"/ca": 1.0}})   # 1쪽: 리셋만
    _set_extgstate(pdf, 1, {"/g0": {"/ca": 0.4}})   # 2쪽: 투명
    r = _run(_save(pdf, tmp_path / "two_pages.pdf"))
    assert r.status == CheckStatus.WARN
    assert r.pages == [1]
    assert all(s["page"] == 1 for s in r.measured["transparent_states"])


def test_no_resources_passes(tmp_path: Path):
    """/Resources 없는 빈 페이지에서도 예외 없이 pass."""
    pdf = _new_pdf()
    page = pdf.pages[0]
    if "/Resources" in page.obj:
        del page.obj["/Resources"]
    r = _run(_save(pdf, tmp_path / "no_res.pdf"))
    assert r.status == CheckStatus.PASS
