"""데모용 '정상/하자 쇼케이스 세트' 생성기 — 애즈랜드 낱장 인쇄물 8종.

사용자가 데모 화면에 드래그해 올려볼 수 있도록, 사람이 알아보는 한국어 파일명으로
정상 8종 + 대표 하자 12종(단일) + 복합 하자 3종을 data/samples/showcase/ 에 만든다.

설계 원칙(코퍼스와 동일):
- PDF 를 사후 mutate 하지 않는다. generate_clean.generate(defects=..., order=...) 가
  처음부터 그 상태로 생성하고, 결함 파라미터가 곧 정답(ground truth)이다.
- 생성 직후 core.preflight.engine.run_preflight 로 자체 검판한다(adversarial):
    · 정상 파일 → 관문 통과(gate_ok, uncertain/fail 0건). sticker/label 은 칼선·재단선
      안전여백까지 pass.
    · 하자 파일 → 의도한 결함 체크가 반드시 pass 가 아닌 상태(fail/warn/uncertain).
  불일치가 있으면 예외로 즉시 중단한다(임계값이 아니라 생성물을 정답에 맞춘다).
- 인덱스(목록.json)와 사람이 읽는 README.md 를 함께 남긴다. 예상판정은 실제 검판
  리포트에서 도출해 항상 실물과 일치한다.

재현성: generate 가 결정론적이므로 같은 코드로 재실행하면 동일한 세트가 나온다.
CLI: python -m synth.showcase
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.preflight.engine import OrderContext, run_preflight
from core.preflight.report import CheckStatus, PreflightReport
from synth.generate_clean import PRODUCTS, PROJECT_ROOT, generate
from synth.manifest import OrderSpec

SHOWCASE_DIR = PROJECT_ROOT / "data" / "samples" / "showcase"

#: 상품키 → 한국어 상품명 (신규 3종 포함, 과제 고정값)
PRODUCT_KO: dict[str, str] = {
    "sticker": "스티커",
    "namecard": "명함",
    "flyer": "전단",
    "poster": "포스터",
    "label": "라벨",
    "postcard": "엽서",
    "memopad": "떡메모지",
    "photocard": "포토카드",
}

#: 결함키 → 자연스러운 한국어 결함명 (사용자 친화적)
DEFECT_KO: dict[str, str] = {
    "bleed": "재단 여백 부족",
    "resolution": "이미지 저해상도",
    "colorspace": "화면용 RGB 색상",
    "font_embed": "폰트 미포함",
    "trim_safety": "재단선에 걸친 글자",
    "ink_total": "잉크량 과다",
    "black_type": "4도 혼합 검정 본문",
    "page_size": "주문 규격과 크기 불일치",
    "page_count": "페이지 수 불일치",
    "transparency": "투명 효과 사용",
    "dieline": "칼선(도무송) 없음",
    "min_line": "너무 가는 선",
}

#: 검판 체크키 → 한국어 항목명 (리포트 요약용, roles.CHECK_NAMES 와 동일한 표기)
CHECK_KO: dict[str, str] = {
    "bleed": "재단 여백",
    "resolution": "이미지 해상도",
    "colorspace": "색상 모드",
    "font_embed": "폰트 포함(임베딩)",
    "trim_safety": "재단선 안전 여백",
    "ink_total": "총 잉크량",
    "black_type": "검정 표현",
    "page_size": "페이지 크기",
    "page_count": "페이지 수",
    "transparency": "투명 효과",
    "dieline": "칼선",
    "min_line": "최소 선 굵기",
}

#: 상태값 → 한국어 (리포트 표기용)
STATUS_KO: dict[str, str] = {
    "pass": "통과",
    "warn": "주의",
    "fail": "불가",
    "uncertain": "확인필요",
}

# ---------------------------------------------------------------- 쇼케이스 계획
# 각 항목: (파일명, 상품키, [ (결함키, 파라미터오버라이드) ... ], 한국어 설명)
# 결함이 빈 리스트면 정상 파일. dieline 결함은 칼선 상품(sticker/label)에만 얹는다.

#: 정상 8종 — 상품마다 1개
CLEAN_PLAN: list[tuple[str, str]] = [
    ("명함_정상.pdf", "namecard"),
    ("스티커_정상.pdf", "sticker"),
    ("전단_정상.pdf", "flyer"),
    ("포스터_정상.pdf", "poster"),
    ("라벨_정상.pdf", "label"),
    ("엽서_정상.pdf", "postcard"),
    ("떡메모지_정상.pdf", "memopad"),
    ("포토카드_정상.pdf", "photocard"),
]

#: 단일 하자 12종 — 결함 12종을 어울리는 상품에 하나씩
SINGLE_PLAN: list[tuple[str, str, list[tuple[str, dict[str, Any]]], str]] = [
    ("명함_여백부족.pdf", "namecard", [("bleed", {})],
     "재단 도련(여백)이 없어 재단 시 가장자리가 흰색으로 잘릴 수 있는 파일이에요."),
    ("전단_저해상도.pdf", "flyer", [("resolution", {})],
     "본문 사진 해상도가 인쇄 기준(300dpi)에 크게 못 미쳐 흐릿하게 인쇄돼요."),
    ("포스터_RGB색상.pdf", "poster", [("colorspace", {"mode": "image"})],
     "화면용 RGB 사진이 들어 있어 인쇄용(CMYK)으로 바꾸면 색이 달라질 수 있어요."),
    ("명함_폰트깨짐.pdf", "namecard", [("font_embed", {})],
     "본문 폰트가 파일에 포함(임베딩)되지 않아 다른 글꼴로 바뀔 수 있어요."),
    ("스티커_재단선글자.pdf", "sticker", [("trim_safety", {})],
     "글자가 재단선 안전여백(3mm) 안쪽까지 들어와 재단 시 잘릴 위험이 있어요."),
    ("포스터_잉크과다.pdf", "poster", [("ink_total", {})],
     "총 잉크량이 300%를 넘어 건조 불량이나 뒷묻음이 생길 수 있어요."),
    ("전단_4도검정본문.pdf", "flyer", [("black_type", {})],
     "본문 검정이 4도 혼합(리치 블랙)이라 작은 글자가 번져 보일 수 있어요."),
    ("명함_규격불일치.pdf", "namecard", [("page_size", {})],
     "파일 재단 크기가 주문하신 규격과 달라요."),
    ("엽서_페이지수오류.pdf", "postcard", [("page_count", {})],
     "파일 페이지 수가 주문 내용과 달라요."),
    ("라벨_투명도.pdf", "label", [("transparency", {})],
     "투명 효과가 쓰여 인쇄 전 병합(플래튼) 처리가 필요하고 겹친 부분 표현이 달라질 수 있어요."),
    ("스티커_칼선없음.pdf", "sticker", [("dieline", {})],
     "모양대로 자르는 재단(도무송)에 필요한 칼선(별색)이 파일에 없어요."),
    ("포토카드_가는선.pdf", "photocard", [("min_line", {})],
     "0.25pt 미만의 초극세선이 있어 인쇄하면 선이 끊기거나 안 보일 수 있어요."),
]

#: 복합 하자 3종 — 실제 반려될 법한 조합 (dieline 은 칼선 상품 전용 규칙 준수)
MULTI_PLAN: list[tuple[str, str, list[tuple[str, dict[str, Any]]], str]] = [
    ("전단_저해상도+RGB.pdf", "flyer",
     [("resolution", {}), ("colorspace", {"mode": "image"})],
     "저해상도 사진에 화면용 RGB 색상까지 겹쳐 있어 흐릿함과 색 변화가 함께 우려돼요."),
    ("명함_여백부족+재단선글자.pdf", "namecard",
     [("bleed", {}), ("trim_safety", {})],
     "재단 여백이 없는 데다 글자까지 재단선에 걸쳐 있어 가장자리 손실 위험이 커요."),
    ("엽서_규격불일치+페이지수오류.pdf", "postcard",
     [("page_size", {}), ("page_count", {})],
     "파일의 재단 크기와 페이지 수가 모두 주문 내용과 달라요."),
]


def _order_for(product: str) -> OrderSpec:
    """상품 기본 규격 1페이지 주문. page_size/page_count 검사의 정답 기준이 된다."""
    return OrderSpec(size_mm=PRODUCTS[product].size_mm, page_count=1)


def _verdict(report: PreflightReport) -> str:
    """실제 검판 리포트 → 데모 예상판정(정상통과|보정가능|담당자확인)."""
    fails = report.failures
    uncertains = report.uncertains
    warns = report.warnings
    if not fails and not uncertains and not warns:
        return "정상통과"
    if uncertains:
        # 의도 판별이 필요한 회색지대(재단선 글자·칼선 등) → 사람이 확인
        return "담당자확인"
    if fails:
        # 자동 보정 가능한 fail(도련 연장 등)만이면 보정가능, 아니면 담당자확인
        return "보정가능" if all(f.autofix.available for f in fails) else "담당자확인"
    return "보정가능"  # 경고만 있는 경우: 진행 가능하나 고지 대상


def _nonpass_summary(report: PreflightReport) -> dict[str, str]:
    """검출된(=pass 아님) 체크만 {한국어항목: 한국어상태}로 요약."""
    out: dict[str, str] = {}
    for r in report.results:
        s = str(r.status)
        if s != "pass":
            out[CHECK_KO.get(r.check_id, r.check_id)] = STATUS_KO.get(s, s)
    return out


def _build_one(
    out_dir: Path,
    name: str,
    product: str,
    defects: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, Any], PreflightReport]:
    """파일 1개 생성 + 자체 검판. 정답과 어긋나면 RuntimeError."""
    order = _order_for(product)
    out_path = out_dir / name
    generate(
        product,
        out_path,
        defects=[{"id": d, "params": p} for d, p in defects],
        order=order,
    )

    report = run_preflight(
        out_path,
        OrderContext(product=product, size_mm=order.size_mm, page_count=order.page_count),
    )
    defect_ids = [d for d, _ in defects]

    # ── adversarial 자체 검증 ─────────────────────────────
    if not defect_ids:
        # 정상: 관문 통과 + uncertain/fail 0건
        if not report.gate_ok or report.failures or report.uncertains:
            bad = [(r.check_id, str(r.status)) for r in report.results if str(r.status) != "pass"]
            raise RuntimeError(f"정상 파일인데 검판 미통과: {name} → {bad}")
        # sticker/label 은 칼선·재단선 안전여백이 반드시 pass 여야 함
        if PRODUCTS[product].dieline:
            for cid in ("dieline", "trim_safety"):
                r = report.by_id(cid)
                if r is None or str(r.status) != "pass":
                    raise RuntimeError(f"{name}: {cid} 가 pass 가 아님({r and r.status})")
    else:
        # 하자: 의도한 결함 체크가 반드시 pass 가 아니어야 함
        for cid in defect_ids:
            r = report.by_id(cid)
            if r is None or str(r.status) == "pass":
                raise RuntimeError(
                    f"하자 파일인데 '{cid}' 결함이 검출되지 않음: {name} "
                    f"(status={r and r.status})"
                )

    entry: dict[str, Any] = {
        "파일명": name,
        "상품": PRODUCT_KO[product],
        "상품키": product,
        "재단크기mm": [PRODUCTS[product].size_mm[0], PRODUCTS[product].size_mm[1]],
        "상태": "정상" if not defect_ids else "하자",
        "결함목록": [DEFECT_KO[d] for d in defect_ids],
        "결함키": defect_ids,
        "한국어설명": (
            "인쇄 기준을 모두 만족하는 정상 파일이에요."
            if not defect_ids
            else _describe(name)
        ),
        "예상판정": _verdict(report),
        "검출된항목": _nonpass_summary(report),
    }
    return entry, report


# 설명 문구를 계획에서 가져오기 위한 조회 테이블 (파일명 → 설명)
_DESC: dict[str, str] = {
    name: desc for name, _p, _d, desc in (*SINGLE_PLAN, *MULTI_PLAN)
}


def _describe(name: str) -> str:
    return _DESC.get(name, "")


def build_showcase(out_dir: str | Path = SHOWCASE_DIR) -> list[dict[str, Any]]:
    """쇼케이스 세트 전체 생성 + 자체 검판 + 인덱스/README 저장. 반환 = 인덱스 엔트리 목록."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.pdf"):
        stale.unlink()  # 계획과 1:1 유지

    entries: list[dict[str, Any]] = []

    for name, product in CLEAN_PLAN:
        entry, _ = _build_one(out_dir, name, product, [])
        entries.append(entry)
    for name, product, defects, _desc in SINGLE_PLAN:
        entry, _ = _build_one(out_dir, name, product, defects)
        entries.append(entry)
    for name, product, defects, _desc in MULTI_PLAN:
        entry, _ = _build_one(out_dir, name, product, defects)
        entries.append(entry)

    _write_index(out_dir, entries)
    _write_readme(out_dir, entries)
    return entries


def _write_index(out_dir: Path, entries: list[dict[str, Any]]) -> None:
    n_clean = sum(1 for e in entries if e["상태"] == "정상")
    n_defect = len(entries) - n_clean
    index = {
        "설명": "애즈랜드 낱장 인쇄물 8종 데모용 정상/하자 테스트 PDF 세트",
        "생성방법": "python -m synth.showcase",
        "요약": {
            "전체": len(entries),
            "정상": n_clean,
            "하자": n_defect,
        },
        "판정범례": {
            "정상통과": "검판 관문을 그대로 통과",
            "보정가능": "자동 보정 또는 고지 후 진행 가능(경고·도련 연장 등)",
            "담당자확인": "의도 확인이 필요하거나 재업로드가 필요(회색지대·규격/페이지 불일치 등)",
        },
        "파일": entries,
    }
    (out_dir / "목록.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_readme(out_dir: Path, entries: list[dict[str, Any]]) -> None:
    n_clean = sum(1 for e in entries if e["상태"] == "정상")
    n_defect = len(entries) - n_clean
    lines: list[str] = []
    lines.append("# 데모용 정상/하자 테스트 PDF 세트")
    lines.append("")
    lines.append(
        "애즈랜드에서 취급하는 낱장 인쇄물 8종(스티커·명함·전단·포스터·라벨·엽서·"
        "떡메모지·포토카드)에 대한 데모용 샘플입니다. 데모 화면에 파일을 그대로 "
        "드래그해 올려 검판 결과를 확인해 보세요."
    )
    lines.append("")
    lines.append(f"- 전체 {len(entries)}개 (정상 {n_clean} / 하자 {n_defect})")
    lines.append("- 재생성: `python -m synth.showcase`")
    lines.append("- 정답·판정 요약은 `목록.json` 참고")
    lines.append("")
    lines.append("## 예상 판정 범례")
    lines.append("")
    lines.append("- **정상통과**: 검판 관문을 그대로 통과합니다.")
    lines.append("- **보정가능**: 자동 보정 또는 고지 후 진행이 가능합니다(경고·도련 연장 등).")
    lines.append(
        "- **담당자확인**: 의도 확인이나 재업로드가 필요합니다"
        "(재단선 글자·칼선·규격/페이지 불일치 등)."
    )
    lines.append("")

    def _table(title: str, rows: list[dict[str, Any]]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 파일명 | 상품 | 결함 | 예상 판정 | 설명 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for e in rows:
            defect = ", ".join(e["결함목록"]) if e["결함목록"] else "-"
            lines.append(
                f"| {e['파일명']} | {e['상품']} | {defect} | {e['예상판정']} | {e['한국어설명']} |"
            )
        lines.append("")

    _table("정상 8종", [e for e in entries if e["상태"] == "정상"])
    _table("단일 하자 12종", [e for e in entries if e["상태"] == "하자" and len(e["결함키"]) == 1])
    _table("복합 하자 3종", [e for e in entries if e["상태"] == "하자" and len(e["결함키"]) >= 2])

    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """CLI: 쇼케이스 세트 전체 재생성 → data/samples/showcase/"""
    entries = build_showcase()
    n_clean = sum(1 for e in entries if e["상태"] == "정상")
    n_defect = len(entries) - n_clean
    print(f"[showcase] {len(entries)}개 생성 → {SHOWCASE_DIR}")
    print(f"[showcase]   정상 {n_clean} / 하자 {n_defect}")
    print(f"[showcase] 인덱스 → {SHOWCASE_DIR / '목록.json'}")
    print(f"[showcase] README → {SHOWCASE_DIR / 'README.md'}")


if __name__ == "__main__":
    main()
