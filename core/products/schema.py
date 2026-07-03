"""상품 슬롯 스키마 — catalog/*.yaml 을 검증해서 로드.

'고객 언어→스펙' 매핑(synonyms)은 스키마 데이터다. 프롬프트에 하드코딩하지 않고
LLM 슬롯 파서 프롬프트에 이 데이터를 주입한다.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

CATALOG_DIR = Path(__file__).parent / "catalog"


class Risk(StrEnum):
    LOW = "low"        # 기본값 통보 후 진행
    MEDIUM = "medium"  # 기본값 제안 + 가벼운 확인
    HIGH = "high"      # 반드시 고객 확정 (틀리면 실물 파손)


class SlotDef(BaseModel):
    display_name: str = ""
    required: bool = False
    infer_from: list[str] = Field(default_factory=list)  # 예: file_trimbox, dieline_present
    default: Any = None
    risk_if_defaulted: Risk = Risk.LOW
    quick_options: list[Any] = Field(default_factory=list)
    synonyms: dict[str, Any] = Field(default_factory=dict)  # "도톰한" → art_300
    ask_if_conflict: bool = False  # 추론값과 고객 발화가 다르면 확인
    choices: list[str] = Field(default_factory=list)  # 허용 값 목록 (비어 있으면 자유값)
    unit: str = ""  # 표시 단위 (예: mm, 매)

    @property
    def has_default(self) -> bool:
        return self.default is not None


class ProductSchema(BaseModel):
    product: str
    display_name: str
    slots: dict[str, SlotDef]
    gates: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "production": ["preflight_all_pass", "customer_confirmed", "no_escalation"]
        }
    )

    def required_slots(self) -> dict[str, SlotDef]:
        return {k: v for k, v in self.slots.items() if v.required}


def load_product(path: str | Path) -> ProductSchema:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ProductSchema.model_validate(data)


def load_catalog(catalog_dir: str | Path = CATALOG_DIR) -> dict[str, ProductSchema]:
    """catalog/*.yaml 전부 로드. 키 = product id."""
    catalog: dict[str, ProductSchema] = {}
    for p in sorted(Path(catalog_dir).glob("*.yaml")):
        schema = load_product(p)
        catalog[schema.product] = schema
    return catalog
