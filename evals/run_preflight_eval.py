"""프리플라이트 자동 채점 하네스.

data/samples/manifest.json(ground truth)과 run_preflight 결과를 대조해
파일 50 × 체크 12 = 600 셀을 채점한다.

셀 정의:
  injected = manifest의 defects에 해당 check_id가 존재
  detected = CheckResult.status != pass  (warn/fail/uncertain 모두 '검출'로 간주)

지표:
  recall = TP / (TP + FN)            목표 >= 0.95
  fpr    = FP / (FP + TN)            목표 <= 0.10

실행:
  .venv\\Scripts\\python.exe -m evals.run_preflight_eval   (프로젝트 루트에서)

출력: 콘솔 표 + evals/reports/preflight_eval.json. 목표 달성 시 exit 0, 아니면 1.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from core.preflight.engine import OrderContext, registered_checks, run_preflight
from core.preflight.report import CheckStatus
from synth.manifest import DEFECT_IDS, Manifest, load_manifest

# 프로젝트 루트 (evals/ 의 부모) — manifest의 상대 경로 기준점
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "data" / "samples" / "manifest.json"
REPORT_PATH = PROJECT_ROOT / "evals" / "reports" / "preflight_eval.json"

RECALL_TARGET = 0.95
FPR_TARGET = 0.10


def _summarize_measured(measured: dict[str, Any], limit: int = 220) -> str:
    """measured 딕셔너리를 미검출/오탐 목록용 한 줄 요약으로 축약."""
    try:
        s = json.dumps(measured, ensure_ascii=False, default=str)
    except Exception:
        s = str(measured)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _ratio(num: int, den: int, default: float) -> float:
    return num / den if den else default


def evaluate(manifest: Manifest) -> dict[str, Any]:
    """전 파일 × 전 체크 채점. 결과를 직렬화 가능한 딕셔너리로 반환."""
    check_ids = sorted(DEFECT_IDS)
    registered = set(registered_checks().keys())
    missing_checks = [cid for cid in check_ids if cid not in registered]
    runnable_ids = [cid for cid in check_ids if cid in registered]

    # 체크별 혼동행렬
    per_check: dict[str, dict[str, int]] = {
        cid: {"tp": 0, "fn": 0, "fp": 0, "tn": 0} for cid in check_ids
    }
    misses: list[dict[str, Any]] = []
    false_positives: list[dict[str, Any]] = []
    file_rows: list[dict[str, Any]] = []
    run_errors: list[str] = []

    for entry in manifest.files:
        pdf_path = PROJECT_ROOT / entry.file
        injected = entry.defect_ids

        if not pdf_path.exists():
            run_errors.append(f"파일 없음: {entry.file}")
            # 존재하지 않는 파일: 주입 결함은 전부 FN, 나머지는 TN으로 처리
            for cid in check_ids:
                if cid in injected:
                    per_check[cid]["fn"] += 1
                    misses.append(
                        {"file": entry.file, "check_id": cid, "status": "missing_file",
                         "detail": "PDF 파일이 존재하지 않음"}
                    )
                else:
                    per_check[cid]["tn"] += 1
            continue

        order = OrderContext(
            product=entry.product,
            size_mm=tuple(entry.order.size_mm),
            page_count=entry.order.page_count,
        )
        t0 = time.monotonic()
        try:
            report = run_preflight(pdf_path, order=order, check_ids=runnable_ids)
            results = {r.check_id: r for r in report.results}
        except Exception as e:  # run_preflight 자체가 죽으면 파일 단위로 격리
            run_errors.append(f"run_preflight 실패: {entry.file}: {type(e).__name__}: {e}")
            results = {}
        elapsed = time.monotonic() - t0

        statuses: dict[str, str] = {}
        for cid in check_ids:
            r = results.get(cid)
            if r is None:
                # 미등록/미실행 체크 = 검출 못 함
                detected = False
                status_str = "not_run"
                measured_summary = ""
                detail = "체크 미등록 또는 결과 누락"
            else:
                detected = r.status != CheckStatus.PASS
                status_str = str(r.status)
                measured_summary = _summarize_measured(r.measured)
                detail = r.detail
            statuses[cid] = status_str

            is_injected = cid in injected
            if is_injected and detected:
                per_check[cid]["tp"] += 1
            elif is_injected and not detected:
                per_check[cid]["fn"] += 1
                misses.append(
                    {"file": entry.file, "check_id": cid, "status": status_str,
                     "measured": measured_summary, "detail": detail}
                )
            elif not is_injected and detected:
                per_check[cid]["fp"] += 1
                false_positives.append(
                    {"file": entry.file, "check_id": cid, "status": status_str,
                     "measured": measured_summary, "detail": detail}
                )
            else:
                per_check[cid]["tn"] += 1

        file_rows.append(
            {"file": entry.file, "product": entry.product,
             "injected": sorted(injected), "statuses": statuses,
             "elapsed_sec": round(elapsed, 2)}
        )

    # 전체 합계
    total = {"tp": 0, "fn": 0, "fp": 0, "tn": 0}
    per_check_out: dict[str, dict[str, Any]] = {}
    for cid, m in per_check.items():
        for k in total:
            total[k] += m[k]
        per_check_out[cid] = {
            **m,
            # 주입 0건인 체크의 recall은 1.0(위반 없음), 음성 0건인 체크의 fpr은 0.0으로 정의
            "recall": round(_ratio(m["tp"], m["tp"] + m["fn"], 1.0), 4),
            "fpr": round(_ratio(m["fp"], m["fp"] + m["tn"], 0.0), 4),
        }

    recall = _ratio(total["tp"], total["tp"] + total["fn"], 1.0)
    fpr = _ratio(total["fp"], total["fp"] + total["tn"], 0.0)
    targets_met = recall >= RECALL_TARGET and fpr <= FPR_TARGET and not missing_checks

    return {
        "manifest": str(MANIFEST_PATH.relative_to(PROJECT_ROOT)),
        "n_files": len(manifest.files),
        "n_checks": len(check_ids),
        "n_cells": len(manifest.files) * len(check_ids),
        "totals": total,
        "recall": round(recall, 4),
        "false_positive_rate": round(fpr, 4),
        "targets": {"recall": RECALL_TARGET, "fpr": FPR_TARGET},
        "targets_met": targets_met,
        "missing_checks": missing_checks,
        "run_errors": run_errors,
        "per_check": per_check_out,
        "misses": misses,
        "false_positives": false_positives,
        "files": file_rows,
    }


def _print_report(res: dict[str, Any]) -> None:
    """콘솔용 표 출력 (사람이 훑는 용도 — 상세는 JSON 참조)."""
    print(f"\n=== 프리플라이트 자동 채점 ===")
    print(f"파일 {res['n_files']} × 체크 {res['n_checks']} = {res['n_cells']} 셀")
    t = res["totals"]
    print(f"전체: TP={t['tp']} FN={t['fn']} FP={t['fp']} TN={t['tn']}  "
          f"recall={res['recall']:.4f} (목표>={res['targets']['recall']})  "
          f"fpr={res['false_positive_rate']:.4f} (목표<={res['targets']['fpr']})")
    print()
    header = f"{'check_id':<14}{'TP':>4}{'FN':>4}{'FP':>4}{'TN':>4}{'recall':>9}{'fpr':>8}"
    print(header)
    print("-" * len(header))
    for cid in sorted(res["per_check"]):
        m = res["per_check"][cid]
        flag = ""
        if m["recall"] < res["targets"]["recall"]:
            flag += " <recall"
        if m["fpr"] > res["targets"]["fpr"]:
            flag += " <fpr"
        print(f"{cid:<14}{m['tp']:>4}{m['fn']:>4}{m['fp']:>4}{m['tn']:>4}"
              f"{m['recall']:>9.4f}{m['fpr']:>8.4f}{flag}")

    if res["missing_checks"]:
        print(f"\n[경고] 미등록 체크: {res['missing_checks']}")
    if res["run_errors"]:
        print(f"\n[경고] 실행 오류 {len(res['run_errors'])}건:")
        for e in res["run_errors"]:
            print(f"  - {e}")

    if res["misses"]:
        print(f"\n미검출(FN) {len(res['misses'])}건:")
        for m in res["misses"]:
            print(f"  - {m['file']} | {m['check_id']} | status={m['status']} | "
                  f"measured={m.get('measured', '')}")
    else:
        print("\n미검출(FN) 없음")

    if res["false_positives"]:
        print(f"\n오탐(FP) {len(res['false_positives'])}건:")
        for m in res["false_positives"]:
            print(f"  - {m['file']} | {m['check_id']} | status={m['status']} | "
                  f"measured={m.get('measured', '')}")
    else:
        print("오탐(FP) 없음")

    print(f"\n목표 달성: {'예' if res['targets_met'] else '아니오'}")


def main() -> int:
    # Windows 콘솔(cp949)에서도 한글 출력이 깨지지 않도록 강제
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    manifest = load_manifest(MANIFEST_PATH)
    res = evaluate(manifest)
    _print_report(res)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n리포트 저장: {REPORT_PATH}")
    return 0 if res["targets_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
