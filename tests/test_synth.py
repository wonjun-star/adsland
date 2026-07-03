"""M1 합성 데이터 팩토리 검증 — ground truth가 물리적으로 성립하는지 pikepdf로 실측.

코퍼스 50종을 임시 디렉터리에 실제로 생성한 뒤:
- manifest 50건 / 구성(정상10·단일25·복합15) / 파일 존재
- 정상 파일의 박스 좌표 (MediaBox = TrimBox + 사방 3mm, BleedBox = MediaBox)
- 결함별 물리 속성 (별색·폰트·이미지 색공간·해상도·잉크·선굵기·투명도·페이지)
- 시드 재실행 시 manifest·PDF 바이트 동일 (재현성)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pikepdf
import pytest

from synth.generate_clean import (
    BLEED_MM,
    PRODUCTS,
    generate,
    has_cutcontour,
    mm_to_pt,
    photo_size_mm,
    pt_to_mm,
)
from synth.inject_defects import build_corpus
from synth.manifest import DEFECT_IDS, Manifest, ManifestEntry, load_manifest

TOL_PT = 0.05  # 박스 좌표 허용 오차 (pt) — 0.5mm 검사 임계보다 훨씬 엄격


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> tuple[Path, Manifest]:
    root = tmp_path_factory.mktemp("synth_corpus")
    manifest = build_corpus(root, root / "manifest.json")
    return root, manifest


def _pdf(root: Path, entry: ManifestEntry) -> Path:
    return root / Path(entry.file).name


def _boxes(page) -> dict[str, list[float]]:
    return {k: [float(v) for v in page.obj[k]] for k in ("/MediaBox", "/TrimBox", "/BleedBox")}


def _file_size_mm(entry: ManifestEntry) -> tuple[float, float]:
    """파일의 실제 재단 크기 (page_size 결함이면 params가 실측값)."""
    d = entry.defect("page_size")
    if d is not None:
        w, h = d.params["file_size_mm"]
        return float(w), float(h)
    return entry.order.size_mm


def _fonts_embedded(pdf: pikepdf.Pdf) -> list[tuple[str, bool]]:
    out = []
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        fonts = res.get("/Font") if res is not None else None
        if fonts is None:
            continue
        for _n, f in fonts.items():
            fd = f.get("/FontDescriptor")
            if fd is None and "/DescendantFonts" in f:
                fd = f["/DescendantFonts"][0].get("/FontDescriptor")
            emb = fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
            out.append((str(f.get("/BaseFont")), bool(emb)))
    return out


def _content_ops(pdf: pikepdf.Pdf) -> tuple[list[float], list[tuple], list[tuple]]:
    """전 페이지 콘텐츠 스트림에서 (선굵기 w, CMYK 채움 k, RGB 채움 rg) 수집."""
    widths, kfills, rgfills = [], [], []
    for page in pdf.pages:
        for operands, op in pikepdf.parse_content_stream(page):
            s = str(op)
            if s == "w":
                widths.append(float(operands[0]))
            elif s == "k":
                kfills.append(tuple(float(v) for v in operands))
            elif s == "rg":
                rgfills.append(tuple(float(v) for v in operands))
    return widths, kfills, rgfills


def _images(pdf: pikepdf.Pdf) -> list[tuple[int, int, str]]:
    """(width_px, height_px, colorspace) — 페이지 리소스의 이미지 XObject."""
    out = []
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        xo = res.get("/XObject") if res is not None else None
        if xo is None:
            continue
        for _n, obj in xo.items():
            if str(obj.get("/Subtype")) == "/Image":
                out.append((int(obj["/Width"]), int(obj["/Height"]), str(obj.get("/ColorSpace"))))
    return out


def _has_low_alpha(pdf: pikepdf.Pdf) -> bool:
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        eg = res.get("/ExtGState") if res is not None else None
        if eg is None:
            continue
        for _n, g in eg.items():
            ca = g.get("/ca")
            CA = g.get("/CA")
            if (ca is not None and float(ca) < 1.0) or (CA is not None and float(CA) < 1.0):
                return True
    return False


# ---------------------------------------------------------------- 구성/존재


def test_corpus_composition(corpus):
    root, manifest = corpus
    assert len(manifest.files) == 50, "코퍼스는 정확히 50건"

    n_clean = sum(1 for e in manifest.files if e.is_clean)
    n_single = sum(1 for e in manifest.files if len(e.defects) == 1)
    n_multi = sum(1 for e in manifest.files if len(e.defects) >= 2)
    assert (n_clean, n_single, n_multi) == (10, 25, 15)

    # 파일 실존 + 이름 유일
    names = [Path(e.file).name for e in manifest.files]
    assert len(set(names)) == 50
    for e in manifest.files:
        assert _pdf(root, e).is_file(), f"파일 없음: {e.file}"

    # 결함 12종 전부 등장
    seen = {d.id for e in manifest.files for d in e.defects}
    assert seen == set(DEFECT_IDS)

    # 복합은 2~3종, dieline은 칼선 상품에만
    for e in manifest.files:
        if len(e.defects) >= 2:
            assert 2 <= len(e.defects) <= 3
        if "dieline" in e.defect_ids:
            assert e.product in ("sticker", "label")

    # 정상은 5상품 × 2
    clean_products = sorted(e.product for e in manifest.files if e.is_clean)
    assert clean_products == sorted(list(PRODUCTS) * 2)


def test_manifest_roundtrip(corpus):
    root, manifest = corpus
    loaded = load_manifest(root / "manifest.json")
    assert loaded == manifest


# ---------------------------------------------------------------- 박스 좌표


def test_clean_boxes(corpus):
    """정상 파일: MediaBox = 재단 + 사방 3mm bleed, TrimBox 중앙, BleedBox = MediaBox."""
    root, manifest = corpus
    b = mm_to_pt(BLEED_MM)
    for e in manifest.files:
        if not e.is_clean:
            continue
        w, h = e.order.size_mm
        tw, th = mm_to_pt(w), mm_to_pt(h)
        with pikepdf.open(_pdf(root, e)) as pdf:
            assert len(pdf.pages) == e.order.page_count
            boxes = _boxes(pdf.pages[0])
            want_media = [0.0, 0.0, tw + 2 * b, th + 2 * b]
            want_trim = [b, b, b + tw, b + th]
            assert all(abs(g - w_) <= TOL_PT for g, w_ in zip(boxes["/MediaBox"], want_media)), e.file
            assert all(abs(g - w_) <= TOL_PT for g, w_ in zip(boxes["/TrimBox"], want_trim)), e.file
            assert boxes["/BleedBox"] == boxes["/MediaBox"], e.file


def test_all_files_box_consistency(corpus):
    """모든 파일: TrimBox 크기 = 파일 실측 규격, bleed 유무는 결함 여부와 일치."""
    root, manifest = corpus
    for e in manifest.files:
        fw, fh = _file_size_mm(e)
        has_bleed_defect = "bleed" in e.defect_ids
        with pikepdf.open(_pdf(root, e)) as pdf:
            for page in pdf.pages:
                boxes = _boxes(page)
                m, t = boxes["/MediaBox"], boxes["/TrimBox"]
                got_w, got_h = pt_to_mm(t[2] - t[0]), pt_to_mm(t[3] - t[1])
                assert abs(got_w - fw) < 0.05 and abs(got_h - fh) < 0.05, e.file
                if has_bleed_defect:
                    assert m == t, f"bleed 결함인데 MediaBox != TrimBox: {e.file}"
                else:
                    margins_mm = [pt_to_mm(v) for v in (t[0] - m[0], t[1] - m[1], m[2] - t[2], m[3] - t[3])]
                    assert all(abs(v - BLEED_MM) < 0.05 for v in margins_mm), e.file


def test_page_size_defect_mismatch(corpus):
    """page_size 결함: 파일 재단 크기가 주문과 0.5mm 초과로 다르다 (주문이 정답)."""
    root, manifest = corpus
    checked = 0
    for e in manifest.files:
        d = e.defect("page_size")
        if d is None:
            continue
        checked += 1
        ow, oh = e.order.size_mm
        fw, fh = (float(v) for v in d.params["file_size_mm"])
        assert abs(fw - ow) > 0.5 or abs(fh - oh) > 0.5
        with pikepdf.open(_pdf(root, e)) as pdf:
            t = _boxes(pdf.pages[0])["/TrimBox"]
            assert abs(pt_to_mm(t[2] - t[0]) - fw) < 0.05
            assert abs(pt_to_mm(t[3] - t[1]) - fh) < 0.05
    assert checked >= 2


def test_page_count_defect_mismatch(corpus):
    """page_count 결함: 파일 페이지 수 != 주문 페이지 수. 그 외 파일은 주문과 일치."""
    root, manifest = corpus
    checked = 0
    for e in manifest.files:
        d = e.defect("page_count")
        with pikepdf.open(_pdf(root, e)) as pdf:
            n = len(pdf.pages)
        if d is None:
            assert n == e.order.page_count, e.file
        else:
            checked += 1
            assert n == int(d.params["file_page_count"])
            assert n != e.order.page_count
    assert checked >= 2


# ---------------------------------------------------------------- 결함별 물리 속성


def test_dieline_ground_truth(corpus):
    """CutContour 별색: 칼선 상품 정상 → 존재 / dieline 결함 → 부재 / 그 외 상품 → 부재."""
    root, manifest = corpus
    for e in manifest.files:
        expected = PRODUCTS[e.product].dieline and "dieline" not in e.defect_ids
        with pikepdf.open(_pdf(root, e)) as pdf:
            assert has_cutcontour(pdf) == expected, e.file


def test_font_embedding(corpus):
    """정상 파일은 전 폰트 임베딩. font_embed 결함 파일은 미임베딩 폰트 존재."""
    root, manifest = corpus
    for e in manifest.files:
        with pikepdf.open(_pdf(root, e)) as pdf:
            fonts = _fonts_embedded(pdf)
        assert fonts, f"폰트 없음: {e.file}"
        if "font_embed" in e.defect_ids:
            assert any(not emb for _, emb in fonts), e.file
        else:
            assert all(emb for _, emb in fonts), e.file


def test_colorspace_defect(corpus):
    """colorspace 결함: image 모드 → DeviceRGB 이미지, fill 모드 → rg 연산자.
    결함 없는 파일: 이미지는 전부 DeviceCMYK, rg 채움 없음."""
    root, manifest = corpus
    for e in manifest.files:
        d = e.defect("colorspace")
        with pikepdf.open(_pdf(root, e)) as pdf:
            images = _images(pdf)
            _, _, rgfills = _content_ops(pdf)
        if d is None:
            assert all(cs == "/DeviceCMYK" for _, _, cs in images), e.file
            assert not rgfills, e.file
        elif d.params["mode"] == "image":
            assert any(cs == "/DeviceRGB" for _, _, cs in images), e.file
        else:  # fill
            assert rgfills, e.file


def test_resolution_ground_truth(corpus):
    """유효 해상도 = 이미지 픽셀폭 ÷ 배치폭(inch). 정상 ≥300dpi, 결함 72~130dpi."""
    root, manifest = corpus
    for e in manifest.files:
        fw, fh = _file_size_mm(e)
        pw_mm, _ = photo_size_mm(fw, fh)
        with pikepdf.open(_pdf(root, e)) as pdf:
            images = _images(pdf)
        assert images, f"사진 없음: {e.file}"
        px_w = max(w for w, _, _ in images)
        eff_dpi = px_w / (pw_mm / 25.4)
        if "resolution" in e.defect_ids:
            assert 72.0 <= eff_dpi <= 130.0, f"{e.file}: {eff_dpi:.1f}dpi"
        else:
            assert eff_dpi >= 300.0, f"{e.file}: {eff_dpi:.1f}dpi"


def test_ink_and_black_ground_truth(corpus):
    """벡터 채움(k 연산자) 잉크 합: 정상 ≤300%, ink_total 결함은 350~400% 채움 존재.
    black_type 결함은 4도 혼합 검정 텍스트 fill 존재."""
    root, manifest = corpus
    for e in manifest.files:
        with pikepdf.open(_pdf(root, e)) as pdf:
            _, kfills, _ = _content_ops(pdf)
        sums = [sum(k) * 100.0 for k in kfills]
        if "ink_total" in e.defect_ids:
            want = e.defect("ink_total").params["total_percent"]
            assert any(350.0 <= s <= 400.0 and abs(s - want) < 0.5 for s in sums), e.file
        else:
            assert all(s <= 300.0 for s in sums), e.file
        if "black_type" in e.defect_ids:
            want_k = tuple(e.defect("black_type").params["cmyk"])
            assert any(all(abs(a - b) < 1e-6 for a, b in zip(k, want_k)) for k in kfills), e.file


def test_min_line_ground_truth(corpus):
    """min_line 결함: 0.25pt 미만 선굵기 존재. 그 외 파일의 모든 스트로크는 ≥0.25pt."""
    root, manifest = corpus
    for e in manifest.files:
        with pikepdf.open(_pdf(root, e)) as pdf:
            widths, _, _ = _content_ops(pdf)
        if "min_line" in e.defect_ids:
            want = float(e.defect("min_line").params["width_pt"])
            assert any(abs(w - want) < 1e-6 for w in widths), e.file
        else:
            assert all(w >= 0.25 for w in widths), e.file


def test_transparency_ground_truth(corpus):
    """transparency 결함: ExtGState ca<1 존재. 그 외 파일에는 없음."""
    root, manifest = corpus
    for e in manifest.files:
        with pikepdf.open(_pdf(root, e)) as pdf:
            low = _has_low_alpha(pdf)
        assert low == ("transparency" in e.defect_ids), e.file


def test_trim_safety_params_recorded(corpus):
    """trim_safety 결함: 위반 인셋(mm)이 3mm 미만으로 params에 기록되어 있다."""
    _, manifest = corpus
    checked = 0
    for e in manifest.files:
        d = e.defect("trim_safety")
        if d is None:
            continue
        checked += 1
        assert 0.0 <= float(d.params["inset_mm"]) < 3.0
    assert checked >= 2


# ---------------------------------------------------------------- 재현성


def test_seed_rerun_reproducible(corpus, tmp_path_factory):
    """같은 시드로 재실행하면 manifest(바이트)와 PDF(sha256)가 완전히 동일하다."""
    root_a, _ = corpus
    root_b = tmp_path_factory.mktemp("synth_corpus_rerun")
    build_corpus(root_b, root_b / "manifest.json")

    a = (root_a / "manifest.json").read_text(encoding="utf-8")
    b = (root_b / "manifest.json").read_text(encoding="utf-8")
    assert a == b, "manifest가 재실행 간 달라짐 — 시드/계획 비결정성"

    for pdf_b in sorted(root_b.glob("*.pdf")):
        pdf_a = root_a / pdf_b.name
        ha = hashlib.sha256(pdf_a.read_bytes()).hexdigest()
        hb = hashlib.sha256(pdf_b.read_bytes()).hexdigest()
        assert ha == hb, f"PDF 바이트 불일치: {pdf_b.name}"


# ---------------------------------------------------------------- 생성기 규칙


def test_dieline_defect_rejected_for_non_dieline_product(tmp_path):
    """dieline 결함은 칼선 상품(sticker/label) 외에는 주입 불가 — GT 보호."""
    with pytest.raises(ValueError):
        generate("namecard", tmp_path / "x.pdf", defects=["dieline"])


def test_duplicate_defect_rejected(tmp_path):
    with pytest.raises(ValueError):
        generate("sticker", tmp_path / "x.pdf", defects=["bleed", "bleed"])
