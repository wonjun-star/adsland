"""카탈로그 9종 + 견적 엔진 테스트.

검증 대상:
  1) catalog/*.yaml 9종이 ProductSchema 로 로드되고 필수 슬롯이 존재한다
  2) 카탈로그의 size/material/coating 값이 pricebook 매트릭스와 1:1 로 일치한다
  3) 견적 조회 성공(기본가 + 후가공 가산 + VAT) / 실패(missing, 추정 금지) 동작
  4) synonyms(고객 언어→스펙)가 스키마 데이터로 로드된다
  5) 신규 3종(엽서/떡메모지/포토카드)이 로드·견적된다
  6) 현수막·배너(banner)가 로드·견적된다 (후가공 finishing 가산, cut_type/sides/coating 없음)
"""

from __future__ import annotations

import yaml
from core.products.schema import Risk, load_catalog
from core.quote.engine import PRICEBOOK_PATH, QuoteResult, quote

# 기존 5종 — coating/sides 등 슬롯 구성이 공통이라 아래 공통 루프가 이 집합을 돈다.
PRODUCT_IDS = {"sticker", "namecard", "flyer", "poster", "label"}
# 신규 3종(낱장, 칼선 없음). 떡메모지는 coating/sides 가 없어 별도 루프로 검증한다.
NEW_PRODUCT_IDS = {"postcard", "memopad", "photocard"}
# 현수막·배너 — finishing addon, cut_type/sides/coating 없음. 공통 루프 밖에서 별도 검증한다.
BANNER_ID = "banner"
ALL_PRODUCT_IDS = PRODUCT_IDS | NEW_PRODUCT_IDS | {BANNER_ID}


def _pricebook() -> dict:
    with open(PRICEBOOK_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ──────────────────────────────── 카탈로그 로드 ────────────────────────────────


def test_catalog_loads_all_products():
    catalog = load_catalog()
    assert set(catalog.keys()) == ALL_PRODUCT_IDS
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
    # pricebook 에 아예 없는 상품 → None (카탈로그-가격표 불일치 → 에스컬레이션 신호)
    assert quote("tumbler", {"size": "600x1800", "quantity": 1}) is None


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


# ════════════════════════════ 신규 3종 (엽서·떡메모지·포토카드) ════════════════════════════


def test_new_products_have_core_and_lineitem_slots():
    """신규 3종의 필수 슬롯 구성 — 낱장이라 cut_type 없음, 상품별 sides/coating 유무 확인."""
    catalog = load_catalog()
    for pid in NEW_PRODUCT_IDS:
        slots = catalog[pid].slots
        for name in ("size", "quantity", "material"):
            assert name in slots and slots[name].required, f"{pid}.{name}"
        assert "cut_type" not in slots, f"{pid}: 낱장 상품엔 재단 형태 슬롯이 없어야 함"
    # 엽서·포토카드: 단/양면 축 존재
    for pid in ("postcard", "photocard"):
        assert set(catalog[pid].slots["sides"].choices) == {"single", "double"}, pid
        assert catalog[pid].slots["coating"].default == "matte", pid
    # 떡메모지: sides/coating 없음, 수량 단위는 '권'
    memo = catalog["memopad"].slots
    assert "sides" not in memo and "coating" not in memo
    assert memo["quantity"].unit == "권"


def test_new_products_sizes_match_pricebook():
    catalog = load_catalog()
    book = _pricebook()
    for pid in NEW_PRODUCT_IDS:
        prices = book["products"][pid]["prices"]
        choices = catalog[pid].slots["size"].choices
        assert sorted(choices) == sorted(prices.keys()), pid


def test_new_products_materials_and_coatings_match_pricebook():
    catalog = load_catalog()
    book = _pricebook()
    for pid in NEW_PRODUCT_IDS:
        entry = book["products"][pid]
        materials = set(catalog[pid].slots["material"].choices)
        for size_key, by_material in entry["prices"].items():
            assert materials == set(by_material.keys()), f"{pid}/{size_key}"
        addon_coatings = set(entry.get("addons", {}).get("coating", {}).keys())
        coating = catalog[pid].slots.get("coating")
        if coating is not None:
            assert set(coating.choices) - {"none"} == addon_coatings, pid
        else:  # 떡메모지: 코팅 슬롯도, addons 도 없어야 한다
            assert addon_coatings == set(), pid


def test_new_products_material_synonyms_within_choices():
    """synonyms 값이 material.choices 안에 있어야 파서 제안이 검증을 통과한다."""
    catalog = load_catalog()
    assert catalog["postcard"].slots["material"].synonyms["랑데뷰"] == "rendezvous_240"
    assert catalog["memopad"].slots["material"].synonyms["모조지"] == "woodfree_100"
    assert catalog["photocard"].slots["material"].synonyms["펄지"] == "pearl_300"
    for pid in NEW_PRODUCT_IDS:
        mat = catalog[pid].slots["material"]
        for word, target in mat.synonyms.items():
            assert target in mat.choices, f"{pid}: '{word}'→{target} 가 choices 에 없음"


def test_new_products_quick_options_are_valid_price_tiers():
    catalog = load_catalog()
    for pid in NEW_PRODUCT_IDS:
        schema = catalog[pid]
        base = {
            "size": schema.slots["size"].choices[0],
            "material": schema.slots["material"].choices[0],
        }
        if "sides" in schema.slots:
            base["sides"] = "single"
        if "coating" in schema.slots:
            base["coating"] = "none"
        for q in schema.slots["quantity"].quick_options:
            result = quote(pid, {**base, "quantity": q})
            assert result is not None and result.ok, f"{pid} qty={q}: {result and result.missing}"


def test_quote_postcard_with_coating_addon():
    result = quote(
        "postcard",
        {"size": "100x148", "material": "snow_250", "sides": "single",
         "quantity": 500, "coating": "matte"},
    )
    assert isinstance(result, QuoteResult)
    assert result.ok and result.missing == []
    # 기본가 16,000 + 무광코팅 10,000 = 공급가 26,000 / VAT 2,600 / 합계 28,600
    assert result.supply_amount == 26000
    assert result.vat == 2600
    assert result.total == 28600
    assert [line.item for line in result.lines] == ["base", "coating:matte"]


def test_quote_memopad_pad_units_no_coating():
    """떡메모지는 권 단위·무코팅 — 내역은 기본가 1줄뿐."""
    result = quote("memopad", {"size": "100x100", "material": "woodfree_100", "quantity": 5})
    assert result is not None and result.ok
    # 기본가 21,000 → VAT 2,100 → 23,100
    assert result.total == 23100
    assert len(result.lines) == 1 and result.lines[0].item == "base"


def test_quote_photocard_double_sides_with_gloss():
    result = quote(
        "photocard",
        {"size": "55x85", "material": "pearl_300", "sides": "double",
         "quantity": 1000, "coating": "gloss"},
    )
    assert result is not None and result.ok
    # 기본가 35,000 + 유광코팅 13,000 = 공급가 48,000 / VAT 4,800 / 합계 52,800
    assert result.supply_amount == 48000
    assert result.total == 52800
    assert [line.item for line in result.lines] == ["base", "coating:gloss"]


def test_quote_memopad_off_tier_quantity_missing():
    """권 단위 수량도 구간 밖(3권)이면 보간하지 않고 missing."""
    result = quote("memopad", {"size": "100x100", "material": "woodfree_100", "quantity": 3})
    assert result is not None
    assert result.missing == ["quantity=3"]
    assert result.total == 0 and result.lines == []


def test_quote_postcard_unknown_size_missing():
    result = quote(
        "postcard",
        {"size": "70x70", "material": "snow_250", "sides": "single", "quantity": 100},
    )
    assert result is not None
    assert result.missing == ["size=70x70"]
    assert result.total == 0 and result.lines == []


def test_quote_photocard_missing_sides_reported():
    """포토카드는 sides 축이 있어 미지정 시 missing 으로 반환."""
    result = quote(
        "photocard", {"size": "55x85", "material": "art_300", "quantity": 100},
    )
    assert result is not None and not result.ok
    assert result.missing == ["sides=(미지정)"]


# ════════════════════════════ 현수막·배너 (실사출력 대형) ════════════════════════════


def test_banner_loads_with_finishing_slot():
    """현수막은 size/quantity/material 필수 + 후가공(finishing) 선택. 낱장 슬롯은 없다."""
    catalog = load_catalog()
    banner = catalog[BANNER_ID]
    assert banner.display_name == "현수막·배너"
    slots = banner.slots
    for name in ("size", "quantity", "material"):
        assert name in slots and slots[name].required, f"banner.{name}"
    for absent in ("cut_type", "sides", "coating"):
        assert absent not in slots, f"banner 에 {absent} 슬롯이 있으면 안 됨"
    fin = slots["finishing"]
    assert fin.required is False
    assert fin.default == "grommet"
    assert set(fin.choices) == {"grommet", "rope", "wood", "none"}
    assert slots["material"].default == "banner_cloth"
    assert slots["quantity"].unit == "장"
    assert slots["quantity"].quick_options == [1, 3, 5]
    # 사이즈는 파일 TrimBox 추론 + 충돌 시 확인 (다른 상품과 동일 규약)
    assert "file_trimbox" in slots["size"].infer_from
    assert slots["size"].ask_if_conflict


def test_banner_sizes_materials_and_finishing_match_pricebook():
    catalog = load_catalog()
    book = _pricebook()
    entry = book["products"][BANNER_ID]
    choices = catalog[BANNER_ID].slots["size"].choices
    assert sorted(choices) == sorted(entry["prices"].keys())
    materials = set(catalog[BANNER_ID].slots["material"].choices)
    for size_key, by_material in entry["prices"].items():
        assert materials == set(by_material.keys()), f"banner/{size_key}"
    # finishing choices(none 제외) == addons.finishing 키
    addon_fin = set(entry.get("addons", {}).get("finishing", {}).keys())
    catalog_fin = set(catalog[BANNER_ID].slots["finishing"].choices) - {"none"}
    assert catalog_fin == addon_fin


def test_banner_synonyms_within_choices():
    catalog = load_catalog()
    mat = catalog[BANNER_ID].slots["material"]
    assert mat.synonyms["방수"] == "banner_cloth"
    assert mat.synonyms["망사"] == "mesh"
    assert mat.synonyms["실내"] == "indoor_pet"
    for word, target in mat.synonyms.items():
        assert target in mat.choices, f"banner material: '{word}'→{target} 가 choices 에 없음"
    fin = catalog[BANNER_ID].slots["finishing"]
    assert fin.synonyms["고리"] == "grommet"
    assert fin.synonyms["로프"] == "rope"
    assert fin.synonyms["각목"] == "wood"
    for word, target in fin.synonyms.items():
        assert target in fin.choices, f"banner finishing: '{word}'→{target} 가 choices 에 없음"


def test_banner_quick_options_are_valid_price_tiers():
    catalog = load_catalog()
    schema = catalog[BANNER_ID]
    base = {
        "size": schema.slots["size"].choices[0],
        "material": schema.slots["material"].choices[0],
        "finishing": "none",
    }
    for q in schema.slots["quantity"].quick_options:
        result = quote(BANNER_ID, {**base, "quantity": q})
        assert result is not None and result.ok, f"banner qty={q}: {result and result.missing}"


def test_quote_banner_with_finishing_addon():
    result = quote(
        "banner",
        {"size": "1800x900", "material": "mesh", "quantity": 3, "finishing": "rope"},
    )
    assert isinstance(result, QuoteResult)
    assert result.ok and result.missing == []
    # 기본가 57,000 + 로프 3,000 = 공급가 60,000 / VAT 6,000 / 합계 66,000
    assert result.supply_amount == 60000
    assert result.vat == 6000
    assert result.total == 66000
    assert [line.item for line in result.lines] == ["base", "finishing:rope"]


def test_quote_banner_grommet_fixed_addon():
    """후가공은 사이즈 무관 고정 가산(int 리프)."""
    result = quote(
        "banner",
        {"size": "900x600", "material": "banner_cloth", "quantity": 1, "finishing": "grommet"},
    )
    assert result is not None and result.ok
    # 기본가 8,000 + 고리 2,000 = 10,000 → VAT 1,000 → 11,000
    assert result.total == 11000
    assert [line.item for line in result.lines] == ["base", "finishing:grommet"]


def test_quote_banner_no_finishing_single_line():
    result = quote(
        "banner",
        {"size": "900x600", "material": "banner_cloth", "quantity": 1, "finishing": "none"},
    )
    assert result is not None and result.ok
    # 후가공 none → 가산 0, 내역 1줄 (기본가 8,000 → VAT 800 → 8,800)
    assert result.total == 8800
    assert len(result.lines) == 1 and result.lines[0].item == "base"


def test_quote_banner_off_tier_quantity_missing():
    """수량 구간 밖(2장)은 보간하지 않고 missing."""
    result = quote("banner", {"size": "900x600", "material": "banner_cloth", "quantity": 2})
    assert result is not None
    assert result.missing == ["quantity=2"]
    assert result.total == 0 and result.lines == []


def test_quote_banner_unknown_size_missing():
    result = quote("banner", {"size": "500x500", "material": "banner_cloth", "quantity": 1})
    assert result is not None
    assert result.missing == ["size=500x500"]
    assert result.total == 0 and result.lines == []
