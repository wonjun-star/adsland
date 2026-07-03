"""콘텐츠 스트림 해석기 — 여러 체크가 공유하는 단일 워커.

페이지의 연산자 흐름을 그래픽 상태(CTM, 색, 선폭)와 함께 걸어가며
체크들이 바로 쓸 수 있는 이벤트 목록으로 변환한다:

- ImageDraw:   이미지 XObject 배치 (픽셀 수 + 배치 크기 → 유효 해상도)
- VectorFill:  벡터 채움 (색공간·성분 → 잉크량, RGB 채움 검출)
- VectorStroke: 스트로크 (유효 선폭 → 최소 선굵기, 별색 칼선)
- TextShow:    텍스트 표시 (fill 색 → 먹1도 검사, 폰트명)

한계(프로토타입 허용): 텍스트 폭은 추정하지 않는다 — 위치가 필요한 검사
(trim_safety)는 pdfium textpage의 문자 박스를 쓸 것. 클리핑은 무시한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pikepdf

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1, 0, 0, 1, 0, 0)


def mat_mul(m1: Matrix, m2: Matrix) -> Matrix:
    """m1 × m2 (PDF 관례: 행벡터, m1 먼저 적용)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def apply_mat(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def mat_scale(m: Matrix) -> float:
    """평균 스케일 (선폭 변환용)."""
    a, b, c, d, _, _ = m
    det = abs(a * d - b * c)
    return math.sqrt(det) if det > 0 else 0.0


@dataclass
class ColorInfo:
    space: str = "DeviceGray"      # DeviceCMYK | DeviceRGB | DeviceGray | Separation:이름 | ICC | Pattern | 기타
    components: tuple = (0.0,)

    @property
    def cmyk_sum_percent(self) -> float | None:
        """DeviceCMYK일 때 잉크 총량(%). 아니면 None."""
        if self.space == "DeviceCMYK" and len(self.components) == 4:
            return sum(self.components) * 100.0
        return None


@dataclass
class ImageDraw:
    page: int
    name: str
    width_px: int
    height_px: int
    colorspace: str               # colorspace_str() 결과
    placed_w_pt: float            # CTM 반영 배치 크기
    placed_h_pt: float
    has_decode: bool
    bits: int = 8

    @property
    def effective_dpi(self) -> float:
        """가로/세로 유효 해상도 중 낮은 쪽."""
        dpis = []
        if self.placed_w_pt > 0:
            dpis.append(self.width_px / (self.placed_w_pt / 72.0))
        if self.placed_h_pt > 0:
            dpis.append(self.height_px / (self.placed_h_pt / 72.0))
        return min(dpis) if dpis else 0.0


@dataclass
class VectorFill:
    page: int
    color: ColorInfo
    bbox: tuple[float, float, float, float] | None  # 디바이스 좌표(pt), 경로 없으면 None


@dataclass
class VectorStroke:
    page: int
    color: ColorInfo
    line_width_pt: float          # CTM 스케일 반영 유효 선폭
    bbox: tuple[float, float, float, float] | None


@dataclass
class TextShow:
    page: int
    color: ColorInfo
    font: str                     # 리소스 폰트 키 (예: /F1)
    font_size_pt: float           # Tm·CTM 스케일 반영
    origin: tuple[float, float]   # 디바이스 좌표(pt)
    raw_len: int                  # 표시 바이트 길이 (폭 추정 금지 — 참고용)
    render_mode: int = 0          # 3 = 보이지 않음


Event = ImageDraw | VectorFill | VectorStroke | TextShow


def colorspace_str(cs_obj, resources=None) -> str:
    """색공간 오브젝트 → 정규화 문자열."""
    if cs_obj is None:
        return "DeviceGray"
    if isinstance(cs_obj, pikepdf.Name):
        name = str(cs_obj).lstrip("/")
        if name in ("DeviceRGB", "DeviceCMYK", "DeviceGray", "Pattern"):
            return name
        # 리소스 /ColorSpace 참조
        if resources is not None and "/ColorSpace" in resources:
            cs_dict = resources["/ColorSpace"]
            if f"/{name}" in cs_dict:
                return colorspace_str(cs_dict[f"/{name}"], resources)
        return name
    if isinstance(cs_obj, pikepdf.Array) and len(cs_obj) > 0:
        head = str(cs_obj[0]).lstrip("/")
        if head == "Separation":
            return f"Separation:{str(cs_obj[1]).lstrip('/')}"
        if head == "ICCBased":
            try:
                n = int(cs_obj[1]["/N"])
            except Exception:
                n = 0
            return {1: "ICC-Gray", 3: "ICC-RGB", 4: "ICC-CMYK"}.get(n, "ICC")
        if head == "Indexed":
            return f"Indexed({colorspace_str(cs_obj[1], resources)})"
        if head == "DeviceN":
            return "DeviceN"
        return head
    return str(cs_obj)


@dataclass
class _GState:
    ctm: Matrix = IDENTITY
    fill: ColorInfo = field(default_factory=ColorInfo)
    stroke: ColorInfo = field(default_factory=ColorInfo)
    line_width: float = 1.0

    def copy(self) -> "_GState":
        return _GState(self.ctm, self.fill, self.stroke, self.line_width)


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def walk_page(
    pdf: pikepdf.Pdf,
    page_index: int,
) -> list[Event]:
    """페이지의 이벤트 목록. Form XObject는 2단계까지 재귀."""
    page = pdf.pages[page_index]
    resources = page.get("/Resources", pikepdf.Dictionary())
    return _walk(page, resources, IDENTITY, page_index, depth=0)


def _walk(stream_obj, resources, base_ctm: Matrix, page_index: int, depth: int) -> list[Event]:
    events: list[Event] = []
    gs = _GState(ctm=base_ctm)
    stack: list[_GState] = []
    path_pts: list[tuple[float, float]] = []
    # 텍스트 상태
    tm: Matrix = IDENTITY
    tlm: Matrix = IDENTITY
    font_name = ""
    font_size = 0.0
    render_mode = 0

    def fnum(v) -> float:
        return float(v)

    try:
        instructions = pikepdf.parse_content_stream(stream_obj)
    except Exception:
        return events

    for operands, operator in instructions:
        op = str(operator)
        try:
            if op == "q":
                stack.append(gs.copy())
            elif op == "Q":
                if stack:
                    gs = stack.pop()
            elif op == "cm" and len(operands) == 6:
                m = tuple(fnum(v) for v in operands)
                gs.ctm = mat_mul(m, gs.ctm)  # type: ignore[arg-type]
            elif op == "w" and operands:
                gs.line_width = fnum(operands[0])
            elif op == "k" and len(operands) == 4:
                gs.fill = ColorInfo("DeviceCMYK", tuple(fnum(v) for v in operands))
            elif op == "K" and len(operands) == 4:
                gs.stroke = ColorInfo("DeviceCMYK", tuple(fnum(v) for v in operands))
            elif op == "g" and operands:
                gs.fill = ColorInfo("DeviceGray", (fnum(operands[0]),))
            elif op == "G" and operands:
                gs.stroke = ColorInfo("DeviceGray", (fnum(operands[0]),))
            elif op == "rg" and len(operands) == 3:
                gs.fill = ColorInfo("DeviceRGB", tuple(fnum(v) for v in operands))
            elif op == "RG" and len(operands) == 3:
                gs.stroke = ColorInfo("DeviceRGB", tuple(fnum(v) for v in operands))
            elif op == "cs" and operands:
                gs.fill = ColorInfo(colorspace_str(operands[0], resources), ())
            elif op == "CS" and operands:
                gs.stroke = ColorInfo(colorspace_str(operands[0], resources), ())
            elif op in ("sc", "scn"):
                comps = tuple(fnum(v) for v in operands if not isinstance(v, pikepdf.Name))
                gs.fill = ColorInfo(gs.fill.space, comps)
            elif op in ("SC", "SCN"):
                comps = tuple(fnum(v) for v in operands if not isinstance(v, pikepdf.Name))
                gs.stroke = ColorInfo(gs.stroke.space, comps)
            elif op == "m" and len(operands) == 2:
                path_pts.append(apply_mat(gs.ctm, fnum(operands[0]), fnum(operands[1])))
            elif op == "l" and len(operands) == 2:
                path_pts.append(apply_mat(gs.ctm, fnum(operands[0]), fnum(operands[1])))
            elif op == "c" and len(operands) == 6:
                for i in (0, 2, 4):
                    path_pts.append(apply_mat(gs.ctm, fnum(operands[i]), fnum(operands[i + 1])))
            elif op in ("v", "y") and len(operands) == 4:
                for i in (0, 2):
                    path_pts.append(apply_mat(gs.ctm, fnum(operands[i]), fnum(operands[i + 1])))
            elif op == "re" and len(operands) == 4:
                x, y, w, h = (fnum(v) for v in operands)
                for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
                    path_pts.append(apply_mat(gs.ctm, px, py))
            elif op in ("f", "f*", "F"):
                events.append(VectorFill(page_index, gs.fill, _bbox(path_pts)))
                path_pts = []
            elif op in ("S", "s"):
                events.append(
                    VectorStroke(page_index, gs.stroke, gs.line_width * mat_scale(gs.ctm), _bbox(path_pts))
                )
                path_pts = []
            elif op in ("B", "B*", "b", "b*"):
                bb = _bbox(path_pts)
                events.append(VectorFill(page_index, gs.fill, bb))
                events.append(
                    VectorStroke(page_index, gs.stroke, gs.line_width * mat_scale(gs.ctm), bb)
                )
                path_pts = []
            elif op == "n":
                path_pts = []
            elif op == "BT":
                tm = tlm = IDENTITY
            elif op == "Tf" and len(operands) == 2:
                font_name = str(operands[0])
                font_size = fnum(operands[1])
            elif op == "Tr" and operands:
                render_mode = int(fnum(operands[0]))
            elif op in ("Td", "TD") and len(operands) == 2:
                tlm = mat_mul((1, 0, 0, 1, fnum(operands[0]), fnum(operands[1])), tlm)
                tm = tlm
            elif op == "Tm" and len(operands) == 6:
                tlm = tm = tuple(fnum(v) for v in operands)  # type: ignore[assignment]
            elif op == "T*":
                tm = tlm
            elif op in ("Tj", "'", '"', "TJ"):
                trm = mat_mul(tm, gs.ctm)
                raw_len = 0
                for od in operands:
                    if isinstance(od, (pikepdf.String, bytes, str)):
                        raw_len += len(bytes(od) if not isinstance(od, str) else od.encode())
                    elif isinstance(od, pikepdf.Array):
                        for item in od:
                            if isinstance(item, pikepdf.String):
                                raw_len += len(bytes(item))
                events.append(
                    TextShow(
                        page=page_index,
                        color=gs.fill,
                        font=font_name,
                        font_size_pt=font_size * mat_scale(trm),
                        origin=apply_mat(trm, 0, 0),
                        raw_len=raw_len,
                        render_mode=render_mode,
                    )
                )
            elif op == "Do" and operands:
                name = str(operands[0])
                xobjs = resources.get("/XObject", None) if resources is not None else None
                if xobjs is not None and name in xobjs:
                    xo = xobjs[name]
                    subtype = str(xo.get("/Subtype", ""))
                    if subtype == "/Image":
                        # 단위 정사각형 → CTM 이 배치 크기
                        w_vec = apply_mat(gs.ctm, 1, 0)
                        o_vec = apply_mat(gs.ctm, 0, 0)
                        h_vec = apply_mat(gs.ctm, 0, 1)
                        placed_w = math.hypot(w_vec[0] - o_vec[0], w_vec[1] - o_vec[1])
                        placed_h = math.hypot(h_vec[0] - o_vec[0], h_vec[1] - o_vec[1])
                        events.append(
                            ImageDraw(
                                page=page_index,
                                name=name,
                                width_px=int(xo.get("/Width", 0)),
                                height_px=int(xo.get("/Height", 0)),
                                colorspace=colorspace_str(xo.get("/ColorSpace"), resources),
                                placed_w_pt=placed_w,
                                placed_h_pt=placed_h,
                                has_decode="/Decode" in xo,
                                bits=int(xo.get("/BitsPerComponent", 8)),
                            )
                        )
                    elif subtype == "/Form" and depth < 2:
                        inner_res = xo.get("/Resources", resources)
                        inner_ctm = gs.ctm
                        if "/Matrix" in xo:
                            fm = tuple(float(v) for v in xo["/Matrix"])
                            inner_ctm = mat_mul(fm, gs.ctm)  # type: ignore[arg-type]
                        events.extend(_walk(xo, inner_res, inner_ctm, page_index, depth + 1))
        except Exception:
            continue  # 개별 연산자 해석 실패는 무시 (보수적)
    return events
