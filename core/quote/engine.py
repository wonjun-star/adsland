"""견적 엔진 — pricebook.yaml 매트릭스 조회. 100% 결정론적.

철칙: 매트릭스에 없는 조합은 절대 추정(보간·근사)하지 않는다.
조회에 실패한 축은 QuoteResult.missing 에 "축=값" 문자열로 담아 반환하고,
오케스트레이터가 이를 질문 또는 에스컬레이션으로 연결한다.

pricebook.yaml 형식 (1:1 계약 — pricebook.yaml 상단 주석 참조):
  products.<product>.axes   : 기본가 조회 축 순서. 마지막 축은 반드시 quantity.
  products.<product>.prices : axes 순서의 중첩 매핑. 리프 = int (원, VAT 별도 공급가).
  products.<product>.addons : 후가공 가산액. addons.<slot>.<value> 가 int(고정) 또는
                              {quantity: 가산액} 매핑. 값 "none"/미지정 → 가산 0.

금액 규약: pricebook 의 모든 금액은 부가세(VAT) 별도 공급가.
  supply_amount = 기본가 + 가산액 합
  vat           = round(supply_amount * vat_rate)   (vat_rate 는 pricebook 정의, 기본 0.1)
  total         = supply_amount + vat               → vat_included=True 의 의미
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

PRICEBOOK_PATH = Path(__file__).parent / "pricebook.yaml"

#: 슬롯이 아예 안 채워진 경우 missing 표기에 쓰는 값 자리표시자
UNSPECIFIED = "(미지정)"

#: 후가공 가산 0 으로 취급하는 값 (코팅 없음 등)
_NO_ADDON_VALUES = (None, "", "none")


class QuoteLine(BaseModel):
    """견적 내역 한 줄. amount 는 원 단위, VAT 별도 공급가."""

    item: str          # 기계 식별자. 예: "base", "coating:matte"
    description: str   # 기계적 요약 (고객 노출 문장은 LLM 이 번역)
    amount: int


class QuoteResult(BaseModel):
    """견적 결과. missing 이 비어 있지 않으면 조회 실패 — total/lines 는 0/빈 값."""

    product: str
    currency: str = "KRW"
    vat_included: bool = True  # total 은 VAT 포함 금액이라는 명시
    supply_amount: int = 0     # 공급가 합 (VAT 별도)
    vat: int = 0               # 부가세
    total: int = 0             # supply_amount + vat (VAT 포함)
    lines: list[QuoteLine] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # 조회 실패 축. 예: ["size=75x75"]
    lead_time: str = ""        # 제작(발주) 기간, 영업일 (예: "2~3")

    @property
    def ok(self) -> bool:
        return not self.missing


#: 상품별 제작(발주) 기간 — 영업일 기준 근사치 (애즈랜드 출고 안내 참고, 데모용).
LEAD_TIME_DAYS: dict[str, str] = {
    "namecard": "2~3",
    "sticker": "3~4",
    "flyer": "2~3",
    "poster": "2~3",
    "label": "4~5",
    "postcard": "2~3",
    "memopad": "3~4",
    "photocard": "3~4",
    "banner": "1~2",
}


@lru_cache(maxsize=8)
def _load_pricebook(path_str: str) -> dict[str, Any]:
    with open(path_str, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _quantity_key(raw: Any) -> int | None:
    """수량 값을 pricebook 의 int 키로 변환. 변환 불가면 None."""
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def quote(
    product: str,
    slots: dict[str, Any],
    pricebook_path: str | Path = PRICEBOOK_PATH,
) -> QuoteResult | None:
    """상품·슬롯 조합으로 pricebook 을 조회해 견적을 만든다.

    반환:
      - None: 상품 자체가 pricebook 에 없음 (카탈로그-가격표 불일치 → 에스컬레이션)
      - QuoteResult(missing=[...]): 축 값이 매트릭스에 없음 → 질문/에스컬레이션 유도
      - QuoteResult(missing=[]): 조회 성공. total 은 VAT 포함 원 단위
    """
    book = _load_pricebook(str(Path(pricebook_path)))
    entry = book.get("products", {}).get(product)
    if entry is None:
        return None

    axes: list[str] = entry["axes"]
    missing: list[str] = []
    axis_values: dict[str, Any] = {}

    # 1) 기본가: axes 순서대로 prices 트리를 내려간다. 실패한 축에서 즉시 중단.
    node: Any = entry["prices"]
    for axis in axes:
        raw = slots.get(axis)
        if raw is None:
            missing.append(f"{axis}={UNSPECIFIED}")
            break
        if axis == "quantity":
            key: Any = _quantity_key(raw)
            if key is None:
                missing.append(f"quantity={raw}")
                break
        else:
            key = str(raw)
        if not isinstance(node, dict) or key not in node:
            missing.append(f"{axis}={raw}")
            break
        axis_values[axis] = key
        node = node[key]

    if missing:
        return QuoteResult(product=product, missing=missing)

    base_amount = int(node)
    spec_summary = ", ".join(f"{a}={axis_values[a]}" for a in axes)
    lines = [QuoteLine(item="base", description=f"기본 인쇄비 ({spec_summary})", amount=base_amount)]

    # 2) 후가공 가산액: addons 에 정의된 슬롯만 본다. "none"/미지정 → 가산 0.
    quantity = axis_values["quantity"]
    for slot_name, table in (entry.get("addons") or {}).items():
        raw = slots.get(slot_name)
        if raw in _NO_ADDON_VALUES:
            continue
        value = str(raw)
        if value not in table:
            missing.append(f"{slot_name}={value}")
            continue
        addon_spec = table[value]
        if isinstance(addon_spec, dict):
            if quantity not in addon_spec:
                # 가산액 표에 해당 수량 구간이 없음 — 추정하지 않는다.
                missing.append(f"{slot_name}={value}@quantity={quantity}")
                continue
            amount = int(addon_spec[quantity])
        else:
            amount = int(addon_spec)
        lines.append(
            QuoteLine(
                item=f"{slot_name}:{value}",
                description=f"후가공 가산 ({slot_name}={value})",
                amount=amount,
            )
        )

    if missing:
        # 일부 축이라도 실패하면 부분 금액을 내보내지 않는다 (오해 방지).
        return QuoteResult(product=product, missing=missing)

    supply = sum(line.amount for line in lines)
    vat_rate = float(book.get("vat_rate", 0.1))
    vat = round(supply * vat_rate)
    return QuoteResult(
        product=product,
        supply_amount=supply,
        vat=vat,
        total=supply + vat,
        lines=lines,
        lead_time=LEAD_TIME_DAYS.get(product, ""),
    )
