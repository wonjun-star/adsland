"""합성 코퍼스 정답(manifest) 스키마와 로드/저장.

manifest.json은 eval의 근간이 되는 ground truth다. 여기 기록된 order(주문 사양)와
defects(주입 결함 파라미터)는 생성 코드(generate_clean.generate)가 보장한다.
evals/run_preflight_eval.py 가 이 모듈을 임포트해 정답 대조에 사용한다.

형식:
{"files": [{"file": "data/samples/corpus/xxx.pdf", "product": "sticker",
            "order": {"size_mm": [90, 90], "page_count": 1},
            "defects": [{"id": "bleed", "params": {}}]}]}

주의: page_size / page_count 결함은 '주문과 파일의 불일치'다.
      기준값(정답)은 항상 order 쪽이며, 파일의 실측값은 defects[].params에 기록된다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

#: 프리플라이트 체크 id와 1:1 대응하는 결함 id 12종 (docs/PLAN.md §6 표)
DEFECT_IDS: frozenset[str] = frozenset(
    {
        "bleed",
        "resolution",
        "colorspace",
        "font_embed",
        "trim_safety",
        "ink_total",
        "black_type",
        "page_size",
        "page_count",
        "dieline",
        "transparency",
        "min_line",
    }
)


class DefectSpec(BaseModel):
    """주입된 결함 1건. id는 프리플라이트 체크 id와 동일해야 한다."""

    id: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _known_id(cls, v: str) -> str:
        if v not in DEFECT_IDS:
            raise ValueError(f"알 수 없는 결함 id: {v} (허용: {sorted(DEFECT_IDS)})")
        return v


class OrderSpec(BaseModel):
    """주문 사양 — page_size/page_count 검사의 기준값 (파일이 아니라 주문이 정답)."""

    size_mm: tuple[float, float]  # 재단 크기 (w, h)
    page_count: int = 1


class ManifestEntry(BaseModel):
    """코퍼스 파일 1건의 정답 라벨."""

    file: str  # 프로젝트 루트 기준 상대 경로 (posix 구분자)
    product: str
    order: OrderSpec
    defects: list[DefectSpec] = Field(default_factory=list)

    @property
    def defect_ids(self) -> set[str]:
        return {d.id for d in self.defects}

    @property
    def is_clean(self) -> bool:
        return not self.defects

    def defect(self, defect_id: str) -> DefectSpec | None:
        for d in self.defects:
            if d.id == defect_id:
                return d
        return None


class Manifest(BaseModel):
    files: list[ManifestEntry] = Field(default_factory=list)

    def by_file(self, file: str) -> ManifestEntry | None:
        """파일 경로(또는 파일명)로 엔트리 조회."""
        name = Path(file).name
        for e in self.files:
            if e.file == file or Path(e.file).name == name:
                return e
        return None


def save_manifest(manifest: Manifest, path: str | Path) -> None:
    """manifest를 JSON으로 저장 (UTF-8, 들여쓰기 2 — 사람이 읽는 정답 문서이기도 하다)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = manifest.model_dump(mode="json")
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_manifest(path: str | Path) -> Manifest:
    """JSON 파일을 읽어 스키마 검증 후 Manifest 반환."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Manifest.model_validate(data)
