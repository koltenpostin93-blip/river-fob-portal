"""
Render the working FOB sheets to a 3-page PDF (Corn p1, Soybeans p2, Wheat p3),
styled to echo the on-screen sheet. Pure-python (reportlab) so it runs on
Streamlit Cloud without system libraries.

The app passes a `sheets` list; each sheet is:
    {"commodity": str, "months": [labels], "rows": [(kind, label, cells), ...]}
where `cells` is a list of (text, is_negative) tuples aligned to the months
(or None for a full-width section header). Formatting/coloring is decided by the
app so this stays a dumb renderer.
"""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                Paragraph, PageBreak)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

JPSI_DARK = colors.HexColor("#32373c")
JPSI_BLUE = colors.HexColor("#0693e3")
NEG_RED = colors.HexColor("#c00000")
SECTION_BG = colors.HexColor("#eef1f4")
GRID = colors.HexColor("#dddddd")


def build_pdf(as_of, sheets):
    buf = io.BytesIO()
    page_w, page_h = landscape(letter)
    top_m, bot_m = 0.4 * inch, 0.35 * inch
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(letter),
        leftMargin=0.4 * inch, rightMargin=0.4 * inch,
        topMargin=top_m, bottomMargin=bot_m,
        title=f"JSA FOB Sheet {as_of:%m-%d-%Y}")
    # Height available for the table body once the title block is accounted for,
    # so each commodity is sized to fit on exactly one page (generous reserve for
    # the title + subtitle flowables so the last row never spills).
    title_block = 56
    table_avail = page_h - top_m - bot_m - title_block
    ss = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=ss["Title"], fontSize=15,
                                 textColor=JPSI_DARK, spaceAfter=2, alignment=0)
    sub_style = ParagraphStyle("s", parent=ss["Normal"], fontSize=9,
                               textColor=colors.grey, spaceAfter=8)

    story = []
    for si, sheet in enumerate(sheets):
        months = sheet["months"]
        ncol = len(months) + 1
        story.append(Paragraph(
            f"JSA FOB Sheet &nbsp;•&nbsp; {sheet['commodity']}", title_style))
        story.append(Paragraph(as_of.strftime("%A, %B %d, %Y"), sub_style))

        nrows = len(sheet["rows"])
        row_h = table_avail / max(1, nrows)             # fill the page exactly
        font_sz = max(5.0, min(7.0, row_h - 3.5))       # keep text inside the row
        pad = max(0.6, (row_h - font_sz) / 2 - 0.8)
        data, cmds = [], [
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), font_sz),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.25, GRID),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ]
        r = 0
        for kind, label, cells in sheet["rows"]:
            if kind == "section" or cells is None:
                is_cash = label.lower().startswith("cash")
                data.append([label] + [""] * (ncol - 1))
                cmds += [
                    ("SPAN", (0, r), (-1, r)),
                    ("BACKGROUND", (0, r), (-1, r),
                     JPSI_BLUE if is_cash else SECTION_BG),
                    ("TEXTCOLOR", (0, r), (-1, r),
                     colors.white if is_cash else JPSI_DARK),
                    ("FONTNAME", (0, r), (-1, r), "Helvetica-Bold"),
                    ("ALIGN", (0, r), (-1, r), "CENTER" if is_cash else "LEFT"),
                ]
            else:
                row = [label]
                for j, (txt, neg) in enumerate(cells):
                    row.append(txt)
                    if neg:
                        cmds.append(("TEXTCOLOR", (j + 1, r), (j + 1, r), NEG_RED))
                data.append(row)
                if kind in ("months", "contracts"):
                    cmds += [("BACKGROUND", (0, r), (-1, r), JPSI_DARK),
                             ("TEXTCOLOR", (0, r), (-1, r), colors.white),
                             ("FONTNAME", (0, r), (-1, r), "Helvetica-Bold")]
                elif kind in ("cbot", "cif", "cash"):
                    cmds.append(("FONTNAME", (0, r), (0, r), "Helvetica-Bold"))
                elif kind == "freight":
                    cmds += [("TEXTCOLOR", (0, r), (0, r), colors.HexColor("#555555")),
                             ("FONTNAME", (0, r), (0, r), "Helvetica-Oblique")]
                elif kind == "topcarry":
                    cmds.append(("FONTNAME", (0, r), (0, r), "Helvetica-Bold"))
            r += 1

        avail = 10.2 * inch
        label_w = 1.7 * inch
        mw = (avail - label_w) / max(1, len(months))
        colw = [label_w] + [mw] * len(months)
        t = Table(data, colWidths=colw, rowHeights=[row_h] * nrows)
        t.setStyle(TableStyle(cmds))
        story.append(t)
        if si < len(sheets) - 1:
            story.append(PageBreak())

    doc.build(story)
    return buf.getvalue()
