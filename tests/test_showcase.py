"""showcase 데모 세트 스팟 테스트 — 정상 8종 검판 통과 + 하자 결함 검출.

build_showcase 는 생성 직후 자체 검판(adversarial)을 수행하므로 정상/하자 정답이
어긋나면 빌드 단계에서 이미 예외가 난다. 여기서는 그 계약을 tmp 디렉터리에서
독립적으로 재확인한다 (기존 data/samples/showcase 는 건드리지 않는다).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.preflight.engine import OrderContext, run_preflight
from synth.generate_clean import PRODUCTS
from synth.showcase import CLEAN_PLAN, MULTI_PLAN, SINGLE_PLAN, build_showcase


@pytest.fixture(scope="module")
def showcase(tmp_path_factory) -> tuple[Path, list[dict]]:
    out = tmp_path_factory.mktemp("showcase")
    entries = build_showcase(out)  # 자체 검판 포함 — 정답 불일치 시 여기서 예외
    return out, entries


def _preflight(out: Path, name: str, product: str):
    spec = PRODUCTS[product]
    return run_preflight(
        out / name,
        OrderContext(product=product, size_mm=spec.size_mm, page_count=1),
    )


def test_counts_and_products(showcase):
    """정상 8 + 하자 15 = 23, 정상 세트에 애즈랜드 8종이 모두 등장."""
    _out, entries = showcase
    assert len(entries) == 23
    assert sum(1 for e in entries if e["상태"] == "정상") == 8
    assert sum(1 for e in entries if e["상태"] == "하자") == 15
    clean_products = {e["상품키"] for e in entries if e["상태"] == "정상"}
    assert clean_products == set(PRODUCTS)


def test_clean_8_pass_gate(showcase):
    """정상 8종은 검판 관문 통과(gate_ok, uncertain/fail 0건). sticker/label 은 칼선·재단선 pass."""
    out, _entries = showcase
    for name, product in CLEAN_PLAN:
        rep = _preflight(out, name, product)
        bad = [(r.check_id, str(r.status)) for r in rep.results if str(r.status) != "pass"]
        assert rep.gate_ok, f"{name}: 관문 미통과 {bad}"
        assert not rep.failures and not rep.uncertains, f"{name}: {bad}"
        if PRODUCTS[product].dieline:  # sticker/label
            assert str(rep.by_id("dieline").status) == "pass", f"{name}: dieline 미통과"
            assert str(rep.by_id("trim_safety").status) == "pass", f"{name}: trim_safety 미통과"


def test_representative_defects_detected(showcase):
    """대표 하자 3종이 해당 결함 체크에서 pass 가 아닌 상태(fail/warn/uncertain)로 잡힌다."""
    out, _entries = showcase
    cases = [
        ("명함_여백부족.pdf", "namecard", "bleed"),
        ("전단_저해상도.pdf", "flyer", "resolution"),
        ("스티커_칼선없음.pdf", "sticker", "dieline"),
    ]
    for name, product, cid in cases:
        rep = _preflight(out, name, product)
        r = rep.by_id(cid)
        assert r is not None and str(r.status) != "pass", f"{name}: '{cid}' 미검출"


def test_all_single_defects_detected(showcase):
    """단일 하자 12종 각각이 의도한 결함을 검출한다 (결함 12종 전부 커버)."""
    out, _entries = showcase
    seen: set[str] = set()
    for name, product, defects, _desc in SINGLE_PLAN:
        cid = defects[0][0]
        seen.add(cid)
        rep = _preflight(out, name, product)
        r = rep.by_id(cid)
        assert r is not None and str(r.status) != "pass", f"{name}: '{cid}' 미검출"
    assert len(seen) == 12


def test_composite_defects_detected(showcase):
    """복합 하자 3종은 조합된 결함이 모두 pass 가 아닌 상태로 잡힌다."""
    out, _entries = showcase
    for name, product, defects, _desc in MULTI_PLAN:
        rep = _preflight(out, name, product)
        for cid, _p in defects:
            r = rep.by_id(cid)
            assert r is not None and str(r.status) != "pass", f"{name}: '{cid}' 미검출"


def test_index_and_readme_written(showcase):
    """인덱스(목록.json)와 README.md 가 생성된다."""
    out, _entries = showcase
    assert (out / "목록.json").is_file()
    assert (out / "README.md").is_file()
