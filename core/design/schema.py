"""명함 시안 콘텐츠 스키마 + 정규화 헬퍼.

'콘텐츠 슬롯'은 사양 슬롯(크기·수량·용지)과 다르다. 사양은 어떻게 인쇄할지,
콘텐츠는 무엇을 인쇄할지다. LLM(또는 규칙 파서)이 자연어에서 이 필드를 뽑아
CardContent 제안을 만들고, 생성기(card.py)가 결정론적으로 PDF를 만든다 (ADR-001).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

#: 시안 생성이 가능한 상품 (프로토타입은 명함만 — 나머지는 파일 업로드/에스컬레이션)
DESIGNABLE_PRODUCTS: frozenset[str] = frozenset({"namecard"})

#: 템플릿 id → 표시 이름
TEMPLATES: dict[str, str] = {
    "modern": "모던 (좌측 컬러 바)",
    "classic": "클래식 (가운데 정렬)",
    "minimal": "미니멀 (여백 강조)",
}
DEFAULT_TEMPLATE = "modern"


class CardContent(BaseModel):
    """명함에 인쇄할 내용. name만 있으면 생성 가능(나머지는 있으면 배치)."""

    name: str = ""            # 이름
    title: str = ""           # 직위/직책 (예: 수석 연구원)
    department: str = ""      # 부서
    company: str = ""         # 회사명
    phone: str = ""           # 휴대폰/대표번호
    tel: str = ""             # 유선
    email: str = ""
    address: str = ""
    tagline: str = ""         # 슬로건 (선택)

    def is_generatable(self) -> bool:
        return bool(self.name.strip() or self.company.strip())

    def filled_fields(self) -> list[str]:
        return [k for k, v in self.model_dump().items() if str(v).strip()]

    def merged_with(self, other: "CardContent") -> "CardContent":
        """other의 비어있지 않은 필드로 덮어쓴 새 CardContent (턴마다 누적 입력)."""
        data = self.model_dump()
        for k, v in other.model_dump().items():
            if str(v).strip():
                data[k] = v
        return CardContent(**data)


_PHONE_RE = re.compile(r"\D+")


def normalize_phone(raw: str) -> str:
    """숫자만 남겨 한국 번호 포맷으로. 실패하면 원문 유지."""
    digits = _PHONE_RE.sub("", raw or "")
    if len(digits) == 11 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10:
        if digits.startswith("02"):
            return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 9 and digits.startswith("02"):
        return f"{digits[:2]}-{digits[2:5]}-{digits[5:]}"
    return raw.strip()
