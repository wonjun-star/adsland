"""trim_safety — 텍스트의 재단선 안전여백(3mm) 침범 검사.

회색지대 체크: 기계적으로 침범은 감지되지만 (재단선까지 걸치는 제목,
가장자리 장식 문구 등) 디자인 의도인지 실수인지 판별할 수 없다.
→ 위반 발견 시 fail이 아니라 **uncertain**으로 보고해 고객 확인 질문으로 넘긴다.

측정 방법:
- pdfium textpage에서 문자 단위 tight bbox를 수집한다 (get_charbox).
  좌표는 페이지 좌표 pt (CropBox==MediaBox 좌표계 — PDF 좌표와 동일).
- 각 문자 박스의 '재단선(4변)까지 최소 거리(mm)'를 계산해 3mm 미만이면 위반.
  재단선 밖(bleed 영역)으로 나간 텍스트는 음수 거리로 함께 잡힌다.
- 텍스트만 본다. 벡터 채움·스트로크·이미지는 절대 검사하지 않는다 —
  배경/밴드는 의도적으로 bleed 끝까지 칠해지므로 그것까지 보면 전 파일 오탐.
- 공백 문자와 폭·높이 0 박스는 무시한다.

연속된 위반 문자는 하나의 run으로 병합해 measured.violations에 담는다
(LLM이 "'CUT EDGE TEXT' 문구가 재단선에서 1mm…"처럼 번역할 수 있도록).
"""

from __future__ import annotations

from core.preflight.engine import CheckContext, pt_to_mm, register_check, result
from core.preflight.report import CheckResult, CheckStatus

SAFE_MARGIN_MM = 3.0  # 재단선 안전여백 기준 (PLAN §6)
TOL_MM = 0.05         # 부동소수·글리프 경계 산출 오차 허용
MAX_REPORT = 20       # measured.violations 최대 항목 수 (초과분은 개수만 기록)


def _iter_chars(tp, raw):
    """textpage의 (index, 문자, tight bbox) 시퀀스. 공백·크기 0 박스는 건너뛴다."""
    n = tp.count_chars()
    for idx in range(n):
        try:
            code = raw.FPDFText_GetUnicode(tp, idx)
            ch = chr(code) if code else ""
            if not ch or ch.isspace():
                continue
            left, bottom, right, top = tp.get_charbox(idx)
        except Exception:
            continue  # 개별 문자 해석 실패는 무시 (보수적)
        if right - left <= 0 or top - bottom <= 0:
            continue
        yield idx, ch, (left, bottom, right, top)


def _min_dist_mm(
    box: tuple[float, float, float, float],
    trim: tuple[float, float, float, float],
) -> float:
    """문자 박스에서 재단선 4변까지의 최소 거리(mm). 음수 = 재단선 밖."""
    left, bottom, right, top = box
    x0, y0, x1, y1 = trim
    return pt_to_mm(min(left - x0, bottom - y0, x1 - right, y1 - top))


@register_check("trim_safety")
def check_trim_safety(ctx: CheckContext) -> CheckResult:
    # 애즈랜드 가이드 품목별 안전여백: 명함·사각스티커 2mm / 도무송·리플렛 3mm (없으면 기본 3mm)
    safe_mm = (
        ctx.order.safety_mm if (ctx.order and ctx.order.safety_mm is not None) else SAFE_MARGIN_MM
    )
    required = {"safe_margin_mm": safe_mm}
    try:
        import pypdfium2.raw as pdfium_raw

        violations: list[dict] = []   # 보고용 (최대 MAX_REPORT)
        total_runs = 0                # 전체 위반 run 수
        bad_pages: set[int] = set()
        no_trim_pages: list[int] = []

        for i in range(ctx.page_count):
            trim = ctx.page_boxes(i).get("trim")
            if trim is None:
                no_trim_pages.append(i)  # 안전영역 기준 자체를 정의할 수 없음
                continue

            page = ctx.pdfium[i]
            tp = page.get_textpage()
            try:
                # 위반 문자를 인접 index끼리 하나의 run으로 병합
                # run = [마지막 index, 문자열, 병합 bbox]
                runs: list[list] = []
                for idx, ch, box in _iter_chars(tp, pdfium_raw):
                    if _min_dist_mm(box, trim) >= safe_mm - TOL_MM:
                        continue
                    if runs and idx - runs[-1][0] <= 2:  # gap 2 = 공백 1자 건너뜀
                        last = runs[-1]
                        gap = " " if idx - last[0] > 1 else ""
                        last[0] = idx
                        last[1] += gap + ch
                        lb = last[2]
                        last[2] = (
                            min(lb[0], box[0]), min(lb[1], box[1]),
                            max(lb[2], box[2]), max(lb[3], box[3]),
                        )
                    else:
                        runs.append([idx, ch, box])
            finally:
                tp.close()

            for _, text, box in runs:
                total_runs += 1
                bad_pages.add(i)
                if len(violations) < MAX_REPORT:
                    violations.append(
                        {
                            "page": i,
                            "text": text,
                            # 재단선까지 최소 이격(mm). 음수면 재단선 밖까지 나감
                            "char_bbox_mm_from_trim": round(_min_dist_mm(box, trim), 2),
                            "bbox_pt": [round(v, 2) for v in box],
                        }
                    )

        measured: dict = {
            "violations": violations,
            "violation_count": total_runs,
            "checked_pages": ctx.page_count,
        }

        if no_trim_pages:
            # TrimBox 없음 → 판단 불가 (fail 아님 — page_size/bleed 체크가 별도 처리)
            measured["pages_without_trimbox"] = no_trim_pages
            return result(
                "trim_safety",
                CheckStatus.UNCERTAIN,
                measured=measured,
                required=required,
                pages=sorted(bad_pages | set(no_trim_pages)),
                detail=f"TrimBox 없는 페이지 {no_trim_pages} — 안전영역 기준 산출 불가",
            )

        if total_runs:
            worst = min(v["char_bbox_mm_from_trim"] for v in violations)
            return result(
                "trim_safety",
                CheckStatus.UNCERTAIN,
                measured=measured,
                required=required,
                pages=sorted(bad_pages),
                detail=(
                    f"텍스트 {total_runs}건이 재단선 {safe_mm:g}mm 안전영역 침범"
                    f" (최소 이격 {worst}mm) — 의도 여부 고객 확인 필요"
                ),
            )

        return result(
            "trim_safety",
            CheckStatus.PASS,
            measured=measured,
            required=required,
            detail=f"모든 텍스트가 재단선에서 {safe_mm:g}mm 이상 안쪽",
        )
    except Exception as e:
        # 체크 내부 예외는 밖으로 내보내지 않는다 — 판단 불가로 격리
        return result(
            "trim_safety",
            CheckStatus.UNCERTAIN,
            required=required,
            detail=f"측정 실패: {type(e).__name__}: {e}",
        )
