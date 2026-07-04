"""명함 시안용 한글 폰트 등록 (나눔고딕, SIL OFL — 재배포 가능).

배포본(포터블 zip·도커)에서 시스템 폰트에 기대지 않도록 폰트를 저장소에 번들한다.
등록은 한 번만 하면 되므로 멱등하게 처리한다.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = Path(__file__).parent / "assets" / "fonts"

#: reportlab에 등록되는 폰트 이름
FONT_REGULAR = "NanumGothic"
FONT_BOLD = "NanumGothic-Bold"

_registered = False


def register_card_fonts() -> tuple[str, str]:
    """(정체, 볼드) 폰트 이름 반환. 여러 번 불러도 안전."""
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont(FONT_REGULAR, str(FONT_DIR / "NanumGothic-Regular.ttf")))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / "NanumGothic-Bold.ttf")))
        _registered = True
    return FONT_REGULAR, FONT_BOLD
