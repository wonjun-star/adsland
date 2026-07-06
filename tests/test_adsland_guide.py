"""애즈랜드 작업 가이드 기반 품목별 검수 — 임계값이 실제로 품목마다 다르게 적용되는지.

가이드 핵심: 재단여백은 명함 1mm / 전단 2mm / 스티커 3mm, 총잉크량은 전단·포스터 250%.
'명함 3mm 요구'로 잘못 걸리던 걸 가이드대로 1mm로 완화한 게 반영됐는지 확인한다.
"""

from __future__ import annotations

import pikepdf
import pytest
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from core.preflight.adsland_guide import PRODUCT_RULES, rule_for, safety_mm_for
from core.preflight.checks.bleed import check_bleed
from core.preflight.engine import CheckContext, OrderContext
from core.preflight.report import CheckStatus


# ----------------------------------------------------------- 규칙 테이블 자체

def test_namecard_bleed_is_1mm():
    """명함 도련은 3mm가 아니라 1mm (인디자인 가이드 + 용지 가이드 작업 92x52 = 재단 90x50 +1mm)."""
    assert rule_for("namecard").bleed_mm == 1.0


def test_flyer_poster_ink_limit_250():
    """전단·포스터는 총잉크량 250% (기본 300%보다 낮음)."""
    assert rule_for("flyer").max_ink_percent == 250.0
    assert rule_for("poster").max_ink_percent == 250.0
    assert rule_for("namecard").max_ink_percent == 300.0


def test_sticker_bleed_3mm():
    assert rule_for("sticker").bleed_mm == 3.0


def test_safety_margin_diecut_vs_square():
    """스티커 안전여백: 사각 2mm / 도무송 3mm."""
    assert safety_mm_for("sticker", "square") == 2.0
    assert safety_mm_for("sticker", "die_cut") == 3.0
    assert safety_mm_for("namecard") == 2.0


def test_unknown_product_uses_default():
    r = rule_for("something_else")
    assert r.bleed_mm is not None  # DEFAULT_RULE


def test_every_catalog_rule_has_source_or_marked_unconfirmed():
    """지어낸 값 방지: 모든 규칙은 근거가 있거나 guide_confirmed=False로 표시돼야 한다."""
    for pid, r in PRODUCT_RULES.items():
        assert r.guide_confirmed or r.note, pid


# ----------------------------------------------------------- 실제 검수 동작

def _pdf_with_bleed(path, cut_w_mm, cut_h_mm, bleed_mm):
    """재단(TrimBox) cut_w×cut_h, 사방 bleed_mm 여백을 둔 MediaBox PDF 생성."""
    media_w = (cut_w_mm + 2 * bleed_mm) * mm
    media_h = (cut_h_mm + 2 * bleed_mm) * mm
    c = canvas.Canvas(str(path), pagesize=(media_w, media_h))
    c.setFillColorRGB(0.1, 0.2, 0.6)
    c.rect(0, 0, media_w, media_h, fill=1, stroke=0)  # 배경 꽉 채움(도련까지)
    c.showPage()
    c.save()
    # TrimBox를 재단 크기로, MediaBox 중앙에 놓는다
    b = bleed_mm * 72 / 25.4
    tw, th = cut_w_mm * 72 / 25.4, cut_h_mm * 72 / 25.4
    pdf = pikepdf.open(str(path), allow_overwriting_input=True)
    pdf.pages[0].TrimBox = [b, b, b + tw, b + th]
    pdf.save(str(path))
    pdf.close()
    return str(path)


def _run_bleed(path, product, size_mm):
    gr = rule_for(product)
    ctx = CheckContext(path)
    order = OrderContext(product=product, size_mm=size_mm, bleed_mm=gr.bleed_mm)
    ctx.order = order
    try:
        return check_bleed(ctx)
    finally:
        ctx.close()


def test_namecard_1mm_bleed_passes(tmp_path):
    """명함 1mm 도련 → 가이드 기준(1mm) 통과. (예전 3mm 기준이면 fail 났을 것)"""
    p = _pdf_with_bleed(tmp_path / "nc1.pdf", 90, 50, 1.0)
    r = _run_bleed(p, "namecard", (90.0, 50.0))
    assert r.status == CheckStatus.PASS, r.detail
    assert r.required["min_mm"] == 1.0


def test_namecard_0mm_bleed_fails(tmp_path):
    """명함이라도 도련 0mm는 fail (자동 보정 대상)."""
    p = _pdf_with_bleed(tmp_path / "nc0.pdf", 90, 50, 0.0)
    r = _run_bleed(p, "namecard", (90.0, 50.0))
    assert r.status == CheckStatus.FAIL
    assert r.autofix.available


def test_sticker_1mm_bleed_fails_but_namecard_passes(tmp_path):
    """같은 1mm 도련이라도 스티커(3mm 요구)는 fail, 명함(1mm 요구)은 pass — 품목별 기준."""
    p = _pdf_with_bleed(tmp_path / "s1.pdf", 90, 50, 1.0)
    sticker = _run_bleed(p, "sticker", (90.0, 50.0))
    namecard = _run_bleed(p, "namecard", (90.0, 50.0))
    assert sticker.status == CheckStatus.FAIL, "스티커는 3mm 필요 → 1mm는 부족"
    assert namecard.status == CheckStatus.PASS, "명함은 1mm면 충분"


def test_flyer_2mm_bleed_passes(tmp_path):
    p = _pdf_with_bleed(tmp_path / "fl2.pdf", 210, 297, 2.0)
    r = _run_bleed(p, "flyer", (210.0, 297.0))
    assert r.status == CheckStatus.PASS, r.detail


def test_offcenter_trimbox_short_side_fails(tmp_path):
    """편측 TrimBox로 한 변 도련이 부족하면, 중심 배치 가정으로 가려지지 않고 fail해야 한다.

    (교차검증에서 나온 버그: order-derived가 실측 편측 부족을 대칭 이상값으로 덮어써 거짓 PASS)
    """
    to_pt = lambda v: v * 72 / 25.4
    mw, mh = 96 * mm, 56 * mm
    p = tmp_path / "offcenter.pdf"
    c = canvas.Canvas(str(p), pagesize=(mw, mh))
    c.setFillColorRGB(0.1, 0.2, 0.6)
    c.rect(0, 0, mw, mh, fill=1, stroke=0)
    c.showPage()
    c.save()
    # 재단 90x50을 왼쪽으로 붙여 배치: 왼쪽 도련 1mm, 오른쪽 5mm (사방 비대칭)
    pdf = pikepdf.open(str(p), allow_overwriting_input=True)
    pdf.pages[0].TrimBox = [to_pt(1), to_pt(3), to_pt(1 + 90), to_pt(3 + 50)]
    pdf.save(str(p))
    pdf.close()
    r = _run_bleed(str(p), "sticker", (90.0, 50.0))  # 스티커 3mm 요구
    assert r.status == CheckStatus.FAIL, f"왼쪽 1mm는 3mm 요구 미달 → fail 이어야: {r.detail}"


@pytest.mark.parametrize("check_id", ["bleed", "colorspace", "resolution", "trim_safety"])
def test_remediation_exists_for_key_checks(check_id):
    """주요 검수 항목엔 가이드 근거 수정 안내가 있어야 한다."""
    from core.preflight.adsland_guide import remediation_for

    rem = remediation_for(check_id)
    assert rem is not None and rem.rule and rem.why
