"""LLM 전송 계층 — 벤더 중립 어댑터.

의존 방향 규칙 (ADR-001): 이 패키지는 orchestrator/DB를 임포트하지 않는다.
LLM은 텍스트를 받고 텍스트(또는 JSON 문자열)를 돌려줄 뿐, 실행 권한이 없다.

모델 티어는 코드가 아니라 설정(TIER_MODELS)으로 관리한다.
ANTHROPIC_API_KEY가 없으면 get_adapter()가 None을 반환하고,
호출측(core/llm/roles.py)은 규칙 기반 폴백으로 동작해야 한다 — 데모는 키 없이도 완주 가능해야 한다.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

#: 역할별 모델. 분류·슬롯 파싱은 구조 추출이라 빠른 Haiku(속도↑, 품질 동일),
#: 자연스러운 대화 생성만 Sonnet 5. 전부 Sonnet으로 쓰려면 MODEL_* 환경변수로 override.
TIER_MODELS: dict[str, str] = {
    "classify": os.environ.get("MODEL_CLASSIFY", "claude-haiku-4-5-20251001"),
    "parse": os.environ.get("MODEL_PARSE", "claude-haiku-4-5-20251001"),
    "dialog": os.environ.get("MODEL_DIALOG", "claude-sonnet-5"),
}


class LLMAdapter(ABC):
    """단일 인터페이스: complete. 도구 호출·상태 변경 없음."""

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],  # [{"role": "user"|"assistant", "content": str}]
        role: str = "dialog",            # TIER_MODELS 키
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str: ...


class AnthropicAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        role: str = "dialog",
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> str:
        # Sonnet 5 등 최신 모델은 temperature를 받지 않는다(deprecated) → 지정된 경우에만 전달
        kwargs: dict = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = self._client.messages.create(
            model=TIER_MODELS[role],
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            **kwargs,
        )
        return "".join(b.text for b in resp.content if b.type == "text")


def get_adapter() -> LLMAdapter | None:
    """키가 있으면 실제 어댑터, 없으면 None (호출측이 규칙 기반 폴백)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicAdapter()
    return None
