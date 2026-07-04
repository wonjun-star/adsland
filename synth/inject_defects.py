"""테스트 코퍼스 빌더 — 정상 10 + 단일결함 25 + 복합결함 15 = 50종 + manifest.

결함 주입은 generate_clean.generate(defects=...) 파라미터로만 한다 (mutate 금지).
코퍼스 구성은 아래 정적 계획(SINGLE_PLAN/MULTI_PLAN)으로 고정되어 있어
시드가 같으면 재실행 시 manifest가 바이트 단위로 동일하다.

배분 원칙:
- 단일 25 = 결함 12종 × 2 + bleed 1 (가장 흔한 결함에 +1). dieline은 sticker/label 전용.
- 복합 15 = 상품 5종 × 3. 물리적으로 호환 가능한 2~3종 조합만
  (예: page_size+page_count 양립, bleed+trim_safety 양립,
   resolution+colorspace(image)는 같은 사진에 저해상도+RGB 동시 적용).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from synth.generate_clean import CORE_PRODUCTS, DEFAULT_SEED, PROJECT_ROOT, generate
from synth.manifest import Manifest, ManifestEntry, save_manifest

#: 정상 변형 수 (상품 5종 × 2 = 10). eval 코퍼스는 CORE_PRODUCTS(기존 5종)로 고정 —
#: generate_clean.PRODUCTS 에 신규 상품이 늘어도 코퍼스/manifest 정답은 그대로여야 한다.
CLEAN_VARIANTS = 2

#: 단일 결함 25종: (결함 id, 상품, 파라미터 오버라이드)
SINGLE_PLAN: list[tuple[str, str, dict[str, Any]]] = [
    ("bleed", "namecard", {}),
    ("bleed", "flyer", {}),
    ("bleed", "sticker", {}),
    ("resolution", "poster", {}),
    ("resolution", "flyer", {}),
    ("colorspace", "namecard", {"mode": "image"}),
    ("colorspace", "poster", {"mode": "fill"}),
    ("font_embed", "flyer", {}),
    ("font_embed", "label", {}),
    ("trim_safety", "namecard", {}),
    ("trim_safety", "sticker", {}),
    ("ink_total", "poster", {}),
    ("ink_total", "flyer", {}),
    ("black_type", "namecard", {}),
    ("black_type", "flyer", {}),
    ("page_size", "sticker", {}),
    ("page_size", "poster", {}),
    ("page_count", "namecard", {}),
    ("page_count", "flyer", {}),
    ("dieline", "sticker", {}),
    ("dieline", "label", {}),
    ("transparency", "poster", {}),
    ("transparency", "label", {}),
    ("min_line", "namecard", {}),
    ("min_line", "sticker", {}),
]

#: 복합 결함 15종: (상품, [(결함 id, 파라미터 오버라이드), ...])
MULTI_PLAN: list[tuple[str, list[tuple[str, dict[str, Any]]]]] = [
    ("sticker", [("bleed", {}), ("trim_safety", {})]),
    ("namecard", [("page_size", {}), ("page_count", {})]),
    ("flyer", [("resolution", {}), ("colorspace", {"mode": "image"})]),
    ("poster", [("ink_total", {}), ("black_type", {})]),
    ("label", [("dieline", {}), ("bleed", {})]),
    ("namecard", [("font_embed", {}), ("trim_safety", {})]),
    ("flyer", [("transparency", {}), ("min_line", {})]),
    ("sticker", [("dieline", {}), ("page_size", {})]),
    ("poster", [("bleed", {}), ("resolution", {})]),
    ("label", [("colorspace", {"mode": "fill"}), ("font_embed", {})]),
    ("namecard", [("black_type", {}), ("min_line", {}), ("trim_safety", {})]),
    ("flyer", [("bleed", {}), ("ink_total", {}), ("transparency", {})]),
    ("poster", [("page_size", {}), ("colorspace", {"mode": "image"})]),
    ("sticker", [("resolution", {}), ("transparency", {}), ("min_line", {})]),
    ("label", [("page_count", {}), ("black_type", {})]),
]

DEFAULT_REL_PREFIX = "data/samples/corpus"


def build_corpus(
    corpus_dir: str | Path,
    manifest_path: str | Path,
    rel_prefix: str = DEFAULT_REL_PREFIX,
    seed: int = DEFAULT_SEED,
) -> Manifest:
    """코퍼스 50종 생성 + manifest 저장. 반환값 = Manifest (정답 라벨 전체).

    manifest의 file 필드는 물리 경로가 아니라 f"{rel_prefix}/{파일명}" (posix) —
    같은 계획+시드면 어디에 생성하든 manifest가 동일해 재현성 검증이 가능하다.
    """
    if len(SINGLE_PLAN) != 25:
        raise RuntimeError(f"단일 결함 계획이 25건이 아님: {len(SINGLE_PLAN)}")
    if len(MULTI_PLAN) != 15:
        raise RuntimeError(f"복합 결함 계획이 15건이 아님: {len(MULTI_PLAN)}")

    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for stale in corpus_dir.glob("*.pdf"):
        stale.unlink()  # 이전 실행 잔재 제거 (코퍼스는 항상 계획과 1:1)

    entries: list[ManifestEntry] = []

    def _emit(name: str, entry: ManifestEntry) -> None:
        entry.file = f"{rel_prefix}/{name}"
        entries.append(entry)

    # 1) 정상 10종 (5상품 × 2변형)
    i = 0
    for product in CORE_PRODUCTS:
        for variant in range(CLEAN_VARIANTS):
            i += 1
            name = f"clean_{i:02d}_{product}_v{variant + 1}.pdf"
            _emit(name, generate(product, corpus_dir / name, variant=variant, seed=seed))

    # 2) 단일 결함 25종
    for i, (defect_id, product, params) in enumerate(SINGLE_PLAN, start=1):
        name = f"single_{i:02d}_{product}_{defect_id}.pdf"
        entry = generate(
            product,
            corpus_dir / name,
            defects=[{"id": defect_id, "params": params}],
            seed=seed,
        )
        _emit(name, entry)

    # 3) 복합 결함 15종 (2~3종 조합)
    for i, (product, combo) in enumerate(MULTI_PLAN, start=1):
        ids_slug = "-".join(d for d, _ in combo)
        name = f"multi_{i:02d}_{product}_{ids_slug}.pdf"
        entry = generate(
            product,
            corpus_dir / name,
            defects=[{"id": d, "params": p} for d, p in combo],
            seed=seed,
        )
        _emit(name, entry)

    manifest = Manifest(files=entries)
    save_manifest(manifest, manifest_path)
    return manifest


def main() -> None:
    """CLI: 코퍼스 50종 → data/samples/corpus/ + data/samples/manifest.json"""
    corpus_dir = PROJECT_ROOT / "data" / "samples" / "corpus"
    manifest_path = PROJECT_ROOT / "data" / "samples" / "manifest.json"
    manifest = build_corpus(corpus_dir, manifest_path)
    n_clean = sum(1 for e in manifest.files if e.is_clean)
    n_single = sum(1 for e in manifest.files if len(e.defects) == 1)
    n_multi = sum(1 for e in manifest.files if len(e.defects) >= 2)
    print(f"[synth] 코퍼스 {len(manifest.files)}건 생성 → {corpus_dir}")
    print(f"[synth]   정상 {n_clean} / 단일결함 {n_single} / 복합결함 {n_multi}")
    print(f"[synth] manifest → {manifest_path}")


if __name__ == "__main__":
    main()
