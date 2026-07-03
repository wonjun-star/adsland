"""프리플라이트 구조화 리포트 스키마.

측정은 100% 결정론적 코드가 한다. 이 스키마의 값은 숫자와 상태뿐이며,
고객에게 보여줄 한국어 문장은 LLM이 이 리포트를 '번역'해서 만든다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"            # 진행 가능하나 고지 필요
    FAIL = "fail"            # 관문 차단 — autofix 또는 재업로드 필요
    UNCERTAIN = "uncertain"  # 기계적 위반 감지, 의도 판별 불가 → 질문/에스컬레이션


class AutofixInfo(BaseModel):
    available: bool = False
    fix_id: str = ""          # 예: "extend_bleed"
    note: str = ""            # 예: "가장자리 픽셀 연장 방식, 육안 차이 없음"


class CheckResult(BaseModel):
    check_id: str
    status: CheckStatus
    measured: dict[str, Any] = Field(default_factory=dict)   # 실측값
    required: dict[str, Any] = Field(default_factory=dict)   # 기준값
    autofix: AutofixInfo = Field(default_factory=AutofixInfo)
    pages: list[int] = Field(default_factory=list)           # 문제 페이지 (0-base)
    detail: str = ""                                         # 기계적 요약 (디버깅용, 고객 노출 아님)


class PreflightReport(BaseModel):
    file: str
    results: list[CheckResult] = Field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return all(r.status == CheckStatus.PASS for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == CheckStatus.FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == CheckStatus.WARN]

    @property
    def uncertains(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == CheckStatus.UNCERTAIN]

    @property
    def gate_ok(self) -> bool:
        """생산 관문 기준: fail 0건 그리고 uncertain 0건(해소 전까지 차단)."""
        return not self.failures and not self.uncertains

    def by_id(self, check_id: str) -> CheckResult | None:
        for r in self.results:
            if r.check_id == check_id:
                return r
        return None
