"""프리플라이트 체크 러너.

체크 1개 = checks/ 아래 파일 1개 = @register_check 함수 1개.
모든 체크는 CheckContext만 받아 CheckResult를 반환한다 (부수효과 금지).

렌더링·PDF 열기는 비용이 크므로 CheckContext가 지연 계산 후 캐시한다.
좌표 단위는 pt(1/72inch)로 통일하고, mm 변환 헬퍼를 쓴다.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.preflight.report import CheckResult, PreflightReport

if TYPE_CHECKING:
    import pikepdf
    import pypdfium2
    from PIL import Image

MM_PER_PT = 25.4 / 72.0
PT_PER_MM = 72.0 / 25.4

#: 렌더링 기반 검사(잉크량 등)에 쓰는 공통 배율. 1pt = 2px (=144dpi)
RENDER_SCALE = 2.0


def pt_to_mm(v: float) -> float:
    return v * MM_PER_PT


def mm_to_pt(v: float) -> float:
    return v * PT_PER_MM


class OrderContext:
    """주문 정보 중 검사에 필요한 부분 (page_size, page_count 검사용)."""

    def __init__(
        self,
        product: str | None = None,
        size_mm: tuple[float, float] | None = None,  # 재단 규격 (w, h)
        page_count: int | None = None,
        cut_type: str | None = None,  # square|circle|die_cut — 칼선 필요 여부 판정용
    ):
        self.product = product
        self.size_mm = size_mm
        self.page_count = page_count
        self.cut_type = cut_type


class CheckContext:
    """체크 함수에 넘겨지는 유일한 입력. PDF 열기/렌더링은 지연 + 캐시."""

    def __init__(self, pdf_path: str | Path, order: OrderContext | None = None):
        self.pdf_path = Path(pdf_path)
        self.order = order or OrderContext()

    @cached_property
    def pdf(self) -> "pikepdf.Pdf":
        import pikepdf

        return pikepdf.open(self.pdf_path)

    @cached_property
    def pdfium(self) -> "pypdfium2.PdfDocument":
        import pypdfium2 as pdfium

        return pdfium.PdfDocument(str(self.pdf_path))

    @cached_property
    def page_count(self) -> int:
        return len(self.pdf.pages)

    def page_boxes(self, page_index: int) -> dict[str, tuple[float, float, float, float]]:
        """페이지의 박스들 (x0, y0, x1, y1) pt. TrimBox 없으면 MediaBox로 폴백하되 키는 구분."""
        page = self.pdf.pages[page_index]
        boxes: dict[str, tuple[float, float, float, float]] = {}
        for key, name in [
            ("/MediaBox", "media"),
            ("/TrimBox", "trim"),
            ("/BleedBox", "bleed"),
            ("/CropBox", "crop"),
        ]:
            if key in page:
                x0, y0, x1, y1 = (float(v) for v in page[key])
                boxes[name] = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        return boxes

    def trim_size_mm(self, page_index: int = 0) -> tuple[float, float] | None:
        """재단 크기(mm). TrimBox 우선, 없으면 None (page_size 체크가 처리)."""
        boxes = self.page_boxes(page_index)
        box = boxes.get("trim")
        if box is None:
            return None
        return (pt_to_mm(box[2] - box[0]), pt_to_mm(box[3] - box[1]))

    def render_page(self, page_index: int, scale: float = RENDER_SCALE) -> "Image.Image":
        """페이지를 비트맵으로 렌더 (캐시). scale=2.0 → 144dpi."""
        key = (page_index, scale)
        cache = getattr(self, "_render_cache", None)
        if cache is None:
            cache = {}
            self._render_cache = cache
        if key not in cache:
            page = self.pdfium[page_index]
            cache[key] = page.render(scale=scale).to_pil()
        return cache[key]

    def content_events(self, page_index: int) -> list:
        """contentstream.walk_page 결과 (캐시). 이미지 배치·잉크량·선폭·텍스트색 검사용."""
        cache = getattr(self, "_events_cache", None)
        if cache is None:
            cache = {}
            self._events_cache = cache
        if page_index not in cache:
            from core.preflight.contentstream import walk_page

            cache[page_index] = walk_page(self.pdf, page_index)
        return cache[page_index]

    def resources(self, page_index: int):
        """페이지 /Resources 딕셔너리 (없으면 빈 딕셔너리)."""
        import pikepdf

        return self.pdf.pages[page_index].get("/Resources", pikepdf.Dictionary())

    def close(self) -> None:
        if "pdf" in self.__dict__:
            self.__dict__["pdf"].close()
        if "pdfium" in self.__dict__:
            self.__dict__["pdfium"].close()


CheckFn = Callable[[CheckContext], CheckResult]

_REGISTRY: dict[str, CheckFn] = {}


def register_check(check_id: str) -> Callable[[CheckFn], CheckFn]:
    """체크 등록 데코레이터. check_id는 §6 표의 id와 1:1."""

    def deco(fn: CheckFn) -> CheckFn:
        if check_id in _REGISTRY:
            raise ValueError(f"중복 체크 id: {check_id}")
        _REGISTRY[check_id] = fn
        return fn

    return deco


def _load_all_checks() -> None:
    """checks/ 패키지 아래 모듈 전부 임포트 → 데코레이터가 레지스트리를 채움."""
    from core.preflight import checks as checks_pkg

    for mod in pkgutil.iter_modules(checks_pkg.__path__):
        importlib.import_module(f"core.preflight.checks.{mod.name}")


def registered_checks() -> dict[str, CheckFn]:
    _load_all_checks()
    return dict(_REGISTRY)


def run_preflight(
    pdf_path: str | Path,
    order: OrderContext | None = None,
    check_ids: list[str] | None = None,
) -> PreflightReport:
    """전체(또는 지정) 체크 실행. 체크 하나의 예외가 전체를 죽이지 않는다."""
    _load_all_checks()
    ctx = CheckContext(pdf_path, order)
    ids = check_ids if check_ids is not None else sorted(_REGISTRY.keys())
    results: list[CheckResult] = []
    try:
        for cid in ids:
            fn = _REGISTRY[cid]
            try:
                results.append(fn(ctx))
            except Exception as e:  # 체크 내부 오류 = uncertain으로 격리 (에스컬레이션 대상)
                from core.preflight.report import CheckStatus

                results.append(
                    CheckResult(
                        check_id=cid,
                        status=CheckStatus.UNCERTAIN,
                        detail=f"check crashed: {type(e).__name__}: {e}",
                    )
                )
    finally:
        ctx.close()
    return PreflightReport(file=str(pdf_path), results=results)


def result(check_id: str, status: Any, **kw: Any) -> CheckResult:
    """체크 구현용 축약 생성자."""
    return CheckResult(check_id=check_id, status=status, **kw)
