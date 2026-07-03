"""LLM 출력 → 구조화 제안 검증 (ADR-001의 관문).

LLM이 무엇을 말하든, 이 모듈의 pydantic 모델을 통과한 것만 '제안'으로 인정된다.
검증 실패는 ParseError → 호출측이 1회 재시도, 2회 연속 실패 시 에스컬레이션 시그널.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class CustomerType(StrEnum):
    A = "A"  # 완성 파일 보유 — 최소 질문으로 확정까지
    B = "B"  # 파일은 있으나 수정/보완 필요
    C = "C"  # 파일 없음(시안 필요) — 프로토타입 스코프 밖, 에스컬레이션


class Intent(StrEnum):
    PROVIDE_INFO = "provide_info"    # 사양 정보 제공
    CONFIRM = "confirm"              # 확정/동의
    DENY = "deny"                    # 거절/취소
    CHANGE = "change"                # 이미 준 값 변경
    QUESTION = "question"            # 고객 질문
    CHITCHAT = "chitchat"            # 잡담/인사
    COMPLAINT = "complaint"          # 불만/부정 감정


class ClassifyProposal(BaseModel):
    """분류기 출력: 고객 유형 + 상품 인식."""

    customer_type: CustomerType
    product: str | None = None       # catalog의 product id 또는 None
    confidence_signals: list[str] = Field(default_factory=list)


class SlotProposal(BaseModel):
    """슬롯 파서 출력: 자연어 → 슬롯 값 '제안'. 적용 여부는 오케스트레이터가 결정."""

    intent: Intent
    slots: dict[str, Any] = Field(default_factory=dict)  # slot명 → 제안값 (스키마 대조는 정책이)
    negative_sentiment: bool = False
    confidence_signals: list[str] = Field(default_factory=list)


class ParseError(Exception):
    pass


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    """LLM 응답에서 JSON 오브젝트 추출 (코드펜스/전후 잡문 허용)."""
    m = _FENCE_RE.search(text)
    candidate = m.group(1) if m else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ParseError(f"JSON 없음: {text[:200]!r}")
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON 파싱 실패: {e}") from e


def validate_proposal(text: str, model_cls: type[BaseModel]) -> BaseModel:
    """LLM 텍스트 → 검증된 제안 모델. 실패는 전부 ParseError로 수렴."""
    try:
        return model_cls.model_validate(extract_json(text))
    except ValidationError as e:
        raise ParseError(f"스키마 검증 실패: {e}") from e
