"""카탈로그 5종 + 견적 엔진 테스트.

검증 대상:
  1) catalog/*.yaml 5종이 ProductSchema 로 로드되고 필수 슬롯이 존재한다
  2) 카탈로그의 size/material/coating 값이 pricebook 매트릭스와 1:1 로 일치한다
  3) 견적 조회 성공(기본가 + 후가공 가산 + VAT) / 실패(missing, 추정 금지) 동작
  4) synonyms(고객 언어→스펙)가 스키마 데이터로 로드된다
"""

from __future__ import annotations

import yaml
from core.products.schema import Risk, load_catalog
from core.quote.engine import PRICEBOOK_PATH, QuoteResult, quote

PRODUCT_IDS = {"sticker", "namecard", "flyer", "poster", "label"}


def _pricebook() -> dict:
    with open(PRICEBOOK_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ──────────────────────────────── 카탈로그 로드 ────────────────────────────────


def test_catalog_loads_five_products():
    catalog = load_catalog()
    assert set(catalog.keys()) == PRODUCT_IDS
    # display_name 은 한국어로 채워져 있어야 한다
    for schema in catalog.values():
        assert schema.display_name, schema.product
        assert schema.display_name != schema.product


def test_required_slots_exist_per_product():
    catalog = load_catalog()
    # 전 상품 공통 필수 슬롯
    for pid in PRODUCT_IDS:
        slots = catalog[pid].slots
        for name in ("size", "quantity", "material"):
            assert name in slots, f"{pid}.{name} 누락"
            assert slots[name].required, f"{pid}.{name} 은 required 여야 함"
        assert "coating" in slots, f"{pid}.coating 누락"
    # 상품별 특수 슬롯
    for pid in ("namecard", "flyer"):
        assert "sides" in catalog[pid].slots, f"{pid}.sides 누락"
        assert set(catalog[pid].slots["sides"].choices) == {"single", "double"}
    for pid in ("sticker", "label"):
        assert "cut_type" in catalog[pid].slots, f"{pid}.cut_type 누락"
    for pid in ("poster",):
        assert "cut_type" not in catalog[pid].slots
        assert "sides" not in catalog[pid].slots


def test_size_slot_infers_from_trimbox_and_asks_on_conflict():
    catalog = load_catalog()
    for pid in PRODUCT_IDS:
        size = catalog[pid].slots["size"]
        assert "file_trimbox" in size.infer_from, pid
        assert size.ask_if_conflict, pid
        assert size.choices, f"{pid}.size 는 choices(=pricebook 사이즈)가 필요"


def test_coating_has_default_low_risk():
    catalog = load_catalog()
    for pid in PRODUCT_IDS:
        coating = catalog[pid].slots["coating"]
        assert coating.has_default, pid
        assert coating.risk_if_defaulted == Risk.LOW, pid


def test_cut_type_high_risk_inferred_from_dieline():
    catalog = load_catalog()
    for pid in ("sticker", "label"):
        cut = catalog[pid].slots["cut_type"]
        assert cut.required, pid
        assert "dieline_present" in cut.infer_from, pid
        assert cut.risk_if_defaulted == Risk.HIGH, pid


def test_quantity_has_quick_options():
    catalog = load_catalog()
    for pid in PRODUCT_IDS:
        qty = catalog[pid].slots["quantity"]
        assert qty.quick_options, pid


# ──────────────────────────────── synonyms 로드 ────────────────────────────────


def test_material_synonyms_loaded():
    """'고객 언어→스펙' 매핑이 스키마 데이터로 로드되는지 (한국 인쇄 관용어)."""
    catalog = load_catalog()
    sticker_mat = catalog["sticker"].slots["material"].synonyms
    assert sticker_mat["도톰한"] == "art_300"
    assert sticker_mat["방수 되는"] == "pvc_white"
    assert sticker_mat["고급스러운"] == "art_300_matte"

    label_mat = catalog["label"].slots["material"].synonyms
    assert label_mat["방수"] == "yupo"
    assert label_mat["투명한"] == "clear_pet"

    namecard_mat = catalog["namecard"].slots["material"].synonyms
    assert namecard_mat["고급스러운"] == "vannouveau_210"

    # synonyms 값은 해당 슬롯 choices 안의 값이어야 한다 (파서가 낸 제안이 검증을 통과하도록)
    for pid in PRODUCT_IDS:
        mat = catalog[pid].slots["material"]
        for word, target in mat.synonyms.items():
            assert target in mat.choices, f"{pid}: '{word}'→{target} 가 choices 에 없음"


def test_coating_and_cut_synonyms_loaded():
    catalog = load_catalog()
    sticker = catalog["sticker"].slots
    assert sticker["coating"].synonyms["반짝이는"] == "gloss"
    assert sticker["cut_type"].synonyms["도무송"] == "die_cut"
    assert sticker["cut_type"].synonyms["동그란"] == "circle"
    sides = catalog["namecard"].slots["sides"].synonyms
    assert sides["앞뒤로"] == "double"
    assert sides["한 면만"] == "single"


# ──────────────────── 카탈로그 ↔ pricebook 정합성 ────────────────────


def test_catalog_sizes_match_pricebook():
    """size.choices 의 모든 값이 pricebook 매트릭스에 존재해야 한다 (역방향 포함)."""
    catalog = load_catalog()
    book = _pricebook()
    for pid in PRODUCT_IDS:
        prices = book["products"][pid]["prices"]
        choices = catalog[pid].slots["size"].choices
        assert sorted(choices) == sorted(prices.keys()), pid


def test_catalog_materials_and_coatings_match_pricebook():
    catalog = load_catalog()
    book = _pricebook()
    for pid in PRODUCT_IDS:
        entry = book["products"][pid]
        materials = set(catalog[pid].slots["material"].choices)
        for size_key, by_material in entry["prices"].items():
            assert materials == set(by_material.keys()), f"{pid}/{size_key}"
        # coating choices 중 none 을 뺀 값은 addons 표에 있어야 한다
        addon_coatings = set(entry.get("addons", {}).get("coating", {}).keys())
        catalog_coatings = set(catalog[pid].slots["coating"].choices) - {"none"}
        assert catalog_coatings == addon_coatings, pid


def test_quick_options_are_valid_price_tiers():
    """quantity quick_options 는 반드시 조회 가능한 수량 구간이어야 한다."""
    catalog = load_catalog()
    for pid in PRODUCT_IDS:
        schema = catalog[pid]
        base = {
            "size": schema.slots["size"].choices[0],
            "material": schema.slots["material"].choices[0],
            "coating": "none",
        }
        if "sides" in schema.slots:
            base["sides"] = "single"
        for q in schema.slots["quantity"].quick_options:
            result = quote(pid, {**base, "quantity": q})
            assert result is not None and result.ok, f"{pid} qty={q}: {result and result.missing}"


# ──────────────────────────────── 견적 성공 ────────────────────────────────


def test_quote_sticker_with_coating_addon():
    result = quote(
        "sticker",
        {"size": "70x70", "material": "art_250", "quantity": 500, "coating": "matte",
         "cut_type": "die_cut"},
    )
    assert isinstance(result, QuoteResult)
    assert result.ok and result.missing == []
    assert result.currency == "KRW"
    assert result.vat_included is True
    # 기본가 24,000 + 무광코팅 9,000 = 공급가 33,000 / VAT 3,300 / 합계 36,300
    assert result.supply_amount == 33000
    assert result.vat == 3300
    assert result.total == 36300
    assert [line.item for line in result.lines] == ["base", "coating:matte"]
    assert result.total == result.supply_amount + result.vat


def test_quote_namecard_double_sides_axis():
    result = quote(
        "namecard",
        {"size": "90x50", "material": "snow_250", "sides": "double",
         "quantity": 200, "coating": "none"},
    )
    assert result is not None and result.ok
    # 기본가 9,500 → VAT 950 → 10,450 (코팅 none 은 가산 0, 내역 1줄)
    assert result.total == 10450
    assert len(result.lines) == 1 and result.lines[0].item == "base"


def test_quote_quantity_accepts_string():
    """슬롯 파서가 문자열 수량을 내도 조회가 된다 (추정이 아니라 형변환)."""
    a = quote("poster", {"size": "A2", "material": "art_150", "quantity": 50})
    b = quote("poster", {"size": "A2", "material": "art_150", "quantity": "50"})
    assert a is not None and b is not None
    assert a.ok and b.ok and a.total == b.total == 52000 + 5200


# ──────────────────────────────── 견적 실패 (추정 금지) ────────────────────────────────


def test_quote_unknown_product_returns_none():
    assert quote("banner", {"size": "600x1800", "quantity": 1}) is None


def test_quote_unknown_size_returns_missing():
    result = quote("sticker", {"size": "75x75", "material": "art_250", "quantity": 500})
    assert result is not None
    assert result.missing == ["size=75x75"]
    # 추정 금지: 실패 시 금액·내역을 절대 내보내지 않는다
    assert result.total == 0 and result.lines == []


def test_quote_off_tier_quantity_returns_missing():
    """수량 구간 사이 값(700)은 보간하지 않고 missing 으로 반환."""
    result = quote("sticker", {"size": "50x50", "material": "art_250", "quantity": 700})
    assert result is not None
    assert result.missing == ["quantity=700"]
    assert result.total == 0


def test_quote_unfilled_slot_reported_as_missing():
    result = quote("namecard", {"size": "90x50", "material": "snow_250", "quantity": 200})
    assert result is not None and not result.ok
    assert result.missing == ["sides=(미지정)"]


def test_quote_unsupported_addon_returns_missing():
    """전단 유광 코팅은 pricebook 에 없다 → 가산액을 추정하지 않고 missing."""
    result = quote(
        "flyer",
        {"size": "A4", "material": "art_100", "sides": "double",
         "quantity": 1000, "coating": "gloss"},
    )
    assert result is not None
    assert result.missing == ["coating=gloss"]
    assert result.total == 0 and result.lines == []
