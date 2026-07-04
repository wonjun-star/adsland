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
from core.design.schema import (
    ACCENT_COLORS,
    DEFAULT_ACCENT,
    DEFAULT_TEMPLATE,
    TEMPLATES,
    CardContent,
    normalize_phone,
)
from core.preflight.engine import PT_PER_MM

BLEED_MM = 3.0
SAFE_MM = 3.0  # 재단선 안전여백

# 팔레트 (DeviceCMYK)
INK = CMYKColor(0, 0, 0, 1)            # 먹1도
SUB = CMYKColor(0, 0, 0, 0.55)         # 회색 (K only → 먹1도 계열, 본문 아님)
WHITE = CMYKColor(0, 0, 0, 0)


def _accent(content: CardContent) -> CMYKColor:
    c, m, y, k = ACCENT_COLORS.get(content.accent_color or DEFAULT_ACCENT, ACCENT_COLORS[DEFAULT_ACCENT])
    return CMYKColor(c, m, y, k)


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


def _title_line(content: CardContent) -> str:
    return " · ".join(x for x in (content.department, content.title) if x)


def _draw_modern(c: canvas.Canvas, content: CardContent, reg: str, bold: str, tw: float, th: float) -> None:
    """좌측 컬러 바 + 좌측 정렬. 명함의 가장 무난한 형태."""
    point = _accent(content)
    bx = BLEED_MM * PT_PER_MM
    by = BLEED_MM * PT_PER_MM
    bar_w = 6 * PT_PER_MM
    # 좌측 컬러 바 (bleed 왼쪽 끝까지 내려 재단 안전)
    c.setFillColor(point)
    c.rect(0, 0, bx + bar_w, th + 2 * by, fill=1, stroke=0)

    ox = bx + bar_w + 7 * PT_PER_MM
    top = by + th - 12 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(bold, 15)
    c.drawString(ox, top, content.name or content.company)
    title = _title_line(content)
    if title:
        c.setFillColor(SUB)
        c.setFont(reg, 8)
        c.drawString(ox + _width(c, bold, 15, content.name) + 6, top + 1, title)
    y = top - 8 * PT_PER_MM
    if content.bilingual and (content.name_en or content.title_en):
        c.setFillColor(SUB)
        c.setFont(reg, 7)
        c.drawString(ox, y, "  ".join(x for x in (content.name_en, content.title_en) if x))
        y -= 6 * PT_PER_MM
    else:
        y = top - 13 * PT_PER_MM
    if content.company or content.company_en:
        c.setFillColor(INK)
        c.setFont(bold, 9)
        c.drawString(ox, y, content.company or content.company_en)
        if content.bilingual and content.company_en and content.company:
            c.setFillColor(SUB)
            c.setFont(reg, 6.5)
            c.drawString(ox + _width(c, bold, 9, content.company) + 5, y, content.company_en)
        y -= 6 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(reg, 8)
    for line in _phone_lines(content):
        c.drawString(ox, y, line)
        y -= 4.6 * PT_PER_MM


def _draw_classic(c: canvas.Canvas, content: CardContent, reg: str, bold: str, tw: float, th: float) -> None:
    """가운데 정렬 + 상단 구분선. 격식 있는 형태."""
    point = _accent(content)
    cx = (BLEED_MM * PT_PER_MM) + tw / 2
    by = BLEED_MM * PT_PER_MM
    top = by + th - 14 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(bold, 15)
    c.drawCentredString(cx, top, content.name or content.company)
    y = top - 5.5 * PT_PER_MM
    if content.bilingual and content.name_en:
        c.setFillColor(SUB)
        c.setFont(reg, 7)
        c.drawCentredString(cx, y, content.name_en)
        y -= 4.5 * PT_PER_MM
    title = _title_line(content)
    if title:
        c.setFillColor(SUB)
        c.setFont(reg, 8.5)
        c.drawCentredString(cx, y, title + (f"  {content.title_en}" if content.bilingual and content.title_en else ""))
        y -= 3.5 * PT_PER_MM
    # 구분선
    c.setStrokeColor(point)
    c.setLineWidth(1.2)
    c.line(cx - 12 * PT_PER_MM, y, cx + 12 * PT_PER_MM, y)
    y -= 5.5 * PT_PER_MM
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
    point = _accent(content)
    bx = BLEED_MM * PT_PER_MM
    by = BLEED_MM * PT_PER_MM
    ox = bx + 8 * PT_PER_MM
    c.setFillColor(INK)
    c.setFont(bold, 18)
    c.drawString(ox, by + th - 20 * PT_PER_MM, content.name or content.company)
    sub_y = by + th - 26 * PT_PER_MM
    if content.bilingual and (content.name_en or content.title_en):
        c.setFillColor(point)
        c.setFont(reg, 7.5)
        c.drawString(ox, by + th - 25 * PT_PER_MM, "  ".join(x for x in (content.name_en, content.title_en) if x))
        sub_y = by + th - 30 * PT_PER_MM
    if content.title or content.company:
        c.setFillColor(SUB)
        c.setFont(reg, 8.5)
        c.drawString(ox, sub_y, "  ".join(x for x in (content.title, content.company) if x))
    # 하단 포인트 라인 + 연락처
    c.setStrokeColor(point)
    c.setLineWidth(1.0)
    c.line(ox, by + 10 * PT_PER_MM, bx + tw - 8 * PT_PER_MM, by + 10 * PT_PER_MM)
    c.setFillColor(INK)
    c.setFont(reg, 7.5)
    contacts = [normalize_phone(content.phone) if content.phone else "",
                content.email, normalize_phone(content.tel) if content.tel else ""]
    line = "   ".join(x for x in contacts if x)
    c.drawString(ox, by + 5 * PT_PER_MM, line)


def _draw_back(c: canvas.Canvas, content: CardContent, reg: str, bold: str, tw: float, th: float) -> None:
    """뒷면 — 포인트 색상 전면 패널에 회사명/슬로건을 흰색으로. 명함 뒷면의 정석."""
    point = _accent(content)
    mw, mh = tw + 2 * BLEED_MM * PT_PER_MM, th + 2 * BLEED_MM * PT_PER_MM
    c.setFillColor(point)
    c.rect(0, 0, mw, mh, fill=1, stroke=0)  # bleed 끝까지 (재단 안전)
    cx, cy = mw / 2, mh / 2
    company = content.company or content.name
    if company:
        c.setFillColor(WHITE)
        c.setFont(bold, 13)
        c.drawCentredString(cx, cy + 2 * PT_PER_MM, company)
    sub = content.tagline or content.company_en
    if sub:
        c.setFillColor(WHITE)
        c.setFont(reg, 8)
        c.drawCentredString(cx, cy - 5 * PT_PER_MM, sub)


_TEMPLATES = {"modern": _draw_modern, "classic": _draw_classic, "minimal": _draw_minimal}


def _width(c: canvas.Canvas, font: str, size: float, text: str) -> float:
    return c.stringWidth(text or "", font, size)


def _set_boxes(page, mw: float, th: float, tw: float, bleed: float) -> None:
    mh = th + 2 * bleed
    page["/MediaBox"] = pikepdf.Array([0, 0, round(mw, 3), round(mh, 3)])
    page["/TrimBox"] = pikepdf.Array(
        [round(bleed, 3), round(bleed, 3), round(bleed + tw, 3), round(bleed + th, 3)]
    )
    page["/BleedBox"] = pikepdf.Array([0, 0, round(mw, 3), round(mh, 3)])


def generate_namecard(
    content: CardContent,
    out_path: str | Path,
    template: str = DEFAULT_TEMPLATE,
    size_mm: tuple[float, float] = (90, 50),
    double_sided: bool = False,
) -> dict:
    """콘텐츠 → 인쇄용 명함 PDF. double_sided면 앞면+뒷면 2페이지.

    반환: {out_path, template, size_mm, fields, pages}.
    """
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
    # 앞면
    c.setFillColor(WHITE)
    c.rect(0, 0, mw, mh, fill=1, stroke=0)  # 흰 배경을 bleed 끝까지
    _TEMPLATES[template](c, content, reg, bold, tw, th)
    c.showPage()
    # 뒷면 (양면일 때만)
    if double_sided:
        c.setFont(reg, 10)  # showPage 후 폰트 리셋 방지
        _draw_back(c, content, reg, bold, tw, th)
        c.showPage()
    c.save()

    # 박스 확정 (reportlab 박스 API 대신 pikepdf로 확실하게 — 전 페이지)
    with pikepdf.open(out_path, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            _set_boxes(page, mw, th, tw, bleed)
        pdf.save(out_path)

    return {
        "out_path": str(out_path),
        "template": template,
        "template_name": TEMPLATES[template],
        "size_mm": list(size_mm),
        "fields": content.filled_fields(),
        "pages": 2 if double_sided else 1,
    }
