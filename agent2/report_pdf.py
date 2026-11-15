"""Customer-facing PDF report rendering.

The browser print preview is intentionally not used for downloads: headers, URLs and
page-break behaviour are browser-controlled and make a financial-advice report look
unfinished.  ReportLab's built-in CID font keeps Chinese text portable in the PDF.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from io import BytesIO
import re
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from . import clarify


FONT = "STSong-Light"
TOPICS = (("01", "汇款建议", "remittance"), ("02", "开户建议", "account"),
          ("03", "签证建议", "visa"), ("04", "信用卡建议", "credit_card"))
PROFILE_FIELDS = ("role", "beneficiary", "destination_country", "school", "course",
                  "offer_status", "planned_start_date", "payer", "visa_category")


def _font() -> None:
    if FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(FONT))


def _clean(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    # Never let model instructions, database field names, or transport diagnostics
    # become part of a customer document.
    internal = ("prompt", "trace_id", "field_code", "selected_lifestyle", "fx_mode",
                "模型服务", "确定性规则", "traceback", "sqlstate", "psycopg")
    return fallback if not text or any(word.lower() in text.lower() for word in internal) else text


def _paragraph(text: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(_clean(text, "—")).replace("\n", "<br/>"), style)


def _field_text(code: str, field: dict | None, catalog: dict) -> str:
    if not field or field.get("status") != "confirmed":
        return "待确认"
    value = field.get("value")
    if value is None or value == "":
        return "待确认"
    options = {str(item["value"]): item["label"] for item in catalog.get(code, {}).get("options", [])}
    if isinstance(value, list):
        return "、".join(options.get(str(item), str(item)) for item in value)
    rendered = options.get(str(value), str(value))
    if code in {"school", "course"} and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rendered, re.I):
        # These fields historically stored catalog IDs.  Resolve them only for
        # presentation; an orphaned catalogue entry must not leak its UUID.
        try:
            from . import catalog as school_catalog
            rendered = (school_catalog.school_name(rendered) if code == "school"
                        else school_catalog.program_label(rendered)) or "待确认"
        except Exception:
            rendered = "待确认"
    return rendered


def build_report_pdf(profile: dict, advice_response: dict, plan_response: dict) -> bytes:
    """Render an owned profile snapshot into a stable, directly downloadable A4 PDF."""
    _font()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title="Global Pathway 留学准备方案报告", author="Global Pathway")
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ReportTitle", parent=styles["Title"], fontName=FONT, fontSize=21,
                           leading=30, textColor=colors.HexColor("#201e1a"), spaceAfter=5 * mm)
    kicker = ParagraphStyle("Kicker", parent=styles["Normal"], fontName=FONT, fontSize=8,
                            leading=11, textColor=colors.HexColor("#9d2235"), spaceAfter=3 * mm)
    meta = ParagraphStyle("Meta", parent=styles["Normal"], fontName=FONT, fontSize=8.5,
                          leading=12, textColor=colors.HexColor("#514b42"))
    section = ParagraphStyle("Section", parent=styles["Heading2"], fontName=FONT, fontSize=12,
                             leading=18, textColor=colors.HexColor("#8e1b2a"), spaceBefore=9 * mm,
                             spaceAfter=3 * mm)
    topic = ParagraphStyle("Topic", parent=styles["Heading3"], fontName=FONT, fontSize=10.5,
                           leading=16, textColor=colors.HexColor("#292722"), spaceAfter=2 * mm)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontName=FONT, fontSize=9.3,
                          leading=15, textColor=colors.HexColor("#2d2923"), spaceAfter=2.5 * mm)
    note = ParagraphStyle("Note", parent=body, fontSize=8.4, leading=13, textColor=colors.HexColor("#5b554b"))
    catalog = clarify.field_catalog()
    fields = profile.get("fields") or {}
    name = _field_text("beneficiary", fields.get("beneficiary"), catalog)
    if name == "待确认":
        name = _clean(profile.get("display_name"), "未命名档案")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    story: list = [
        _paragraph("GLOBAL PATHWAY · PRIVATE BANKING", kicker),
        _paragraph(f"{name} · 留学准备方案报告", title),
        Table([[Paragraph("留学准备方案快照", meta), Paragraph(f"生成于 {now}", ParagraphStyle("MetaRight", parent=meta, alignment=TA_RIGHT))]],
              colWidths=[85 * mm, 95 * mm], style=TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1, colors.HexColor("#2c2924")),
                                                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5 * mm)])),
        _paragraph("一 · 留学基本信息", section),
    ]
    rows = []
    for index in range(0, len(PROFILE_FIELDS), 2):
        cells = []
        for code in PROFILE_FIELDS[index:index + 2]:
            cells += [_paragraph(catalog.get(code, {}).get("label", code), note),
                      _paragraph(_field_text(code, fields.get(code), catalog), body)]
        if len(cells) == 2:
            cells += ["", ""]
        rows.append(cells)
    info = Table(rows, colWidths=[24 * mm, 66 * mm, 24 * mm, 66 * mm])
    info.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), .45, colors.HexColor("#d9d1c4")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1ece3")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f1ece3")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm), ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
    ]))
    story += [info, _paragraph("二 · 定制方案", section)]
    advice = advice_response.get("advice") or {}
    for no, label, key in TOPICS:
        item = advice.get(key)
        if not item:
            story += [KeepTogether([_paragraph(f"{no} · {label}", topic), _paragraph("尚未开始；您可以在对话中提出相关问题，系统会逐步补全这部分方案。", note)])]
            continue
        payload = item.get("payload") or {}
        summary = _clean(payload.get("summary"), "这部分建议仍在整理中，请以已核验资料为准。")
        block = [_paragraph(f"{no} · {label}", topic), _paragraph(summary, body)]
        for step in (payload.get("steps") or [])[:6]:
            clean = _clean(step)
            if clean:
                block.append(_paragraph(f"• {clean}", body))
        materials = payload.get("materials") or []
        for material in materials[:8]:
            item_name = _clean(material.get("name_zh"))
            if item_name:
                condition = _clean(material.get("condition_text"))
                block.append(_paragraph(f"• {item_name}{'：' + condition if condition else ''}", note))
        # Keep the topic heading attached to its opening paragraph, but never force a
        # long material list onto the next page and leave an artificial blank area.
        story += [KeepTogether(block[:2]), *block[2:], Spacer(1, 2 * mm)]
    story += [_paragraph("三 · 我的待办", section)]
    items = plan_response.get("items") or []
    if items:
        story += [_paragraph(f"{'☑' if item.get('done') else '☐'}  {_clean(item.get('text'))}", body) for item in items]
    else:
        story.append(_paragraph("暂无待办。", note))
    story += [_paragraph("四 · 使用说明", section),
              _paragraph("本报告是当前资料与已发布官方信息的方案快照。政策、产品条款与申请资格可能变化；请以官方最新公布及银行最终审核为准。标注为待确认的信息，建议补充后再使用。", note)]

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.setFillColor(colors.HexColor("#746d61"))
        canvas.drawString(document.leftMargin, 10 * mm, "Global Pathway · 留学准备方案")
        canvas.drawRightString(A4[0] - document.rightMargin, 10 * mm, f"第 {document.page} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
