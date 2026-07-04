"""명함 시안 생성기 — 콘텐츠 필드 → 인쇄용 PDF (100% 결정론적).

보장 사항 (우리 프리플라이트를 그대로 통과하도록 생성):
- MediaBox = 재단 + 사방 3mm bleed, TrimBox = 재단 영역, BleedBox = MediaBox
- 모든 색 DeviceCMYK, 본문 텍스트 먹1도(K100), 배경은 bleed 끝까지
- 폰트 임베딩(나눔고딕 TTF). reportlab 기본 Helvetica가 끼는 것을 initialFontName으로 차단
- 텍스트는 재단선 3mm 안전영역 안쪽에 배치
- 페이지 1장, 90x50mm(기본)

시안 생성물이라 검판은 통과가 당연하지만, 굳이 같은 파이프라인을 태우는 이유는
'생성이든 업로드든 같은 검판을 거친다'는 신뢰를 데모에서 보여주기 위해서다.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
from reportlab.lib.colors import CMYKColor
from reportlab.pdfgen import canvas

from core.design.fonts import register_card_fonts
from core.design.schema import DEFAULT_TEMPLATE, TEMPLATES, CardContent, normalize_phone
from core.preflight.engine import PT_PER_MM

BLEED_MM = 3.0
SAFE_MM = 3.0  # 재단선 안전여백

# 팔레트 (DeviceCMYK) — 포인트 컬러는 진한 청록
INK = CMYKColor(0, 0, 0, 1)            # 먹1도
POINT = CMYKColor(0.82, 0.42, 0.30, 0.10)
SUB = CMYKColor(0, 0, 0, 0.55)         # 회색 (K only → 먹1도 계열, 본문 아님)
WHITE = CMYKColor(0, 0, 0, 0)


def _phone_lines(content: CardContent) -> list[str]:
    lines = []
    if content.phone:
        lines.append(f"M  {normalize_phone(content.phone)}")
    if content.tel:
        lines.append(f"T  {normalize_phone(content.tel)}")
    if content.email:
        lines.append(f"E  {content.email}")
    if content.address:
        lines.append(content.address)
    return lines


def _draw_modern(c: canvas.Canvas, content: CardContent, reg: str, bold: str, tw: float, th: float) -> None:
    """좌측 컬러 바 + 좌측 정렬. 명함의 가장 무난한 형태."""
    bx = BLEED_MM * PT_PER_MM
    by = BLEED_MM * PT_PER_MM
    bar_w = 6 * PT_PER_MM
    # 좌측 컬러 바 (bleed 왼쪽 끝까지 내려 재단 안전)
    c.setFillColor(POINT)
    c.rect(0, 0, bx + bar_w, th + 2 * by, fill=1, stroke=0)

    ox = bx + bar_w + 7 * PT_PER_MM
    top = by + th - 12 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(bold, 15)
    c.drawString(ox, top, content.name or content.company)
    if content.title or content.department:
        c.setFillColor(SUB)
        c.setFont(reg, 8)
        c.drawString(ox + _width(c, bold, 15, content.name) + 6, top + 1,
                     " · ".join(x for x in (content.department, content.title) if x))
    y = top - 13 * PT_PER_MM
    if content.company:
        c.setFillColor(INK)
        c.setFont(bold, 9)
        c.drawString(ox, y, content.company)
        y -= 6 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(reg, 8)
    for line in _phone_lines(content):
        c.drawString(ox, y, line)
        y -= 4.6 * PT_PER_MM


def _draw_classic(c: canvas.Canvas, content: CardContent, reg: str, bold: str, tw: float, th: float) -> None:
    """가운데 정렬 + 상단 구분선. 격식 있는 형태."""
    cx = (BLEED_MM * PT_PER_MM) + tw / 2
    by = BLEED_MM * PT_PER_MM
    top = by + th - 15 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(bold, 15)
    c.drawCentredString(cx, top, content.name or content.company)
    if content.title or content.department:
        c.setFillColor(SUB)
        c.setFont(reg, 8.5)
        c.drawCentredString(cx, top - 6 * PT_PER_MM,
                            " · ".join(x for x in (content.department, content.title) if x))
    # 구분선
    c.setStrokeColor(POINT)
    c.setLineWidth(1.2)
    c.line(cx - 12 * PT_PER_MM, top - 9.5 * PT_PER_MM, cx + 12 * PT_PER_MM, top - 9.5 * PT_PER_MM)
    y = top - 15 * PT_PER_MM
    if content.company:
        c.setFillColor(INK)
        c.setFont(bold, 9)
        c.drawCentredString(cx, y, content.company)
        y -= 5.5 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(reg, 7.8)
    for line in _phone_lines(content):
        c.drawCentredString(cx, y, line)
        y -= 4.4 * PT_PER_MM


def _draw_minimal(c: canvas.Canvas, content: CardContent, reg: str, bold: str, tw: float, th: float) -> None:
    """여백 강조 — 이름 크게, 연락처는 하단 한 줄. 담백한 형태."""
    bx = BLEED_MM * PT_PER_MM
    by = BLEED_MM * PT_PER_MM
    ox = bx + 8 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(bold, 18)
    c.drawString(ox, by + th - 20 * PT_PER_MM, content.name or content.company)
    if content.title or content.company:
        c.setFillColor(SUB)
        c.setFont(reg, 8.5)
        c.drawString(ox, by + th - 26 * PT_PER_MM,
                     "  ".join(x for x in (content.title, content.company) if x))
    # 하단 포인트 라인 + 연락처
    c.setStrokeColor(POINT)
    c.setLineWidth(1.0)
    c.line(ox, by + 10 * PT_PER_MM, bx + tw - 8 * PT_PER_MM, by + 10 * PT_PER_MM)
    c.setFillColor(INK)
    c.setFont(reg, 7.5)
    contacts = [normalize_phone(content.phone) if content.phone else "",
                content.email, normalize_phone(content.tel) if content.tel else ""]
    line = "   ".join(x for x in contacts if x)
    c.drawString(ox, by + 5 * PT_PER_MM, line)


_TEMPLATES = {"modern": _draw_modern, "classic": _draw_classic, "minimal": _draw_minimal}


def _width(c: canvas.Canvas, font: str, size: float, text: str) -> float:
    return c.stringWidth(text or "", font, size)


def generate_namecard(
    content: CardContent,
    out_path: str | Path,
    template: str = DEFAULT_TEMPLATE,
    size_mm: tuple[float, float] = (90, 50),
) -> dict:
    """콘텐츠 → 인쇄용 명함 PDF. 반환: {out_path, template, size_mm, fields}."""
    if template not in _TEMPLATES:
        template = DEFAULT_TEMPLATE
    reg, bold = register_card_fonts()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tw, th = size_mm[0] * PT_PER_MM, size_mm[1] * PT_PER_MM
    bleed = BLEED_MM * PT_PER_MM
    mw, mh = tw + 2 * bleed, th + 2 * bleed

    # initialFontName: reportlab 기본 Helvetica가 리소스에 끼는 것을 차단 (미임베딩 폰트 0)
    c = canvas.Canvas(str(out_path), pagesize=(mw, mh), initialFontName=reg)
    # 배경 흰색을 bleed 끝까지 (재단 넘어가도 안전 — 흰 여백)
    c.setFillColor(WHITE)
    c.rect(0, 0, mw, mh, fill=1, stroke=0)
    _TEMPLATES[template](c, content, reg, bold, tw, th)
    c.showPage()
    c.save()

    # 박스 확정 (reportlab 박스 API 대신 pikepdf로 확실하게)
    with pikepdf.open(out_path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page["/MediaBox"] = pikepdf.Array([0, 0, round(mw, 3), round(mh, 3)])
        page["/TrimBox"] = pikepdf.Array(
            [round(bleed, 3), round(bleed, 3), round(bleed + tw, 3), round(bleed + th, 3)]
        )
        page["/BleedBox"] = pikepdf.Array([0, 0, round(mw, 3), round(mh, 3)])
        pdf.save(out_path)

    return {
        "out_path": str(out_path),
        "template": template,
        "template_name": TEMPLATES[template],
        "size_mm": list(size_mm),
        "fields": content.filled_fields(),
    }
