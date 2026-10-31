"""PDF similarity report (ReportLab, pure Python)."""
from __future__ import annotations

import io
import os
import re
import time
from html import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

PALETTE = [  # (highlight, strong)
    ("#ffd6d6", "#c62828"), ("#d6e4ff", "#1f4fbf"), ("#d8f5d0", "#2e7d32"), ("#ffe7c2", "#c26100"),
    ("#ecd9ff", "#6a1b9a"), ("#cff3f3", "#00796b"), ("#ffd9ef", "#ad1457"), ("#f1eccb", "#7a6400"),
    ("#dfe3ea", "#37474f"), ("#ffdcc9", "#bf360c"), ("#d9f0ff", "#0277bd"), ("#e6f6c8", "#558b2f"),
    ("#f7d9ff", "#8e24aa"), ("#fff3b8", "#9a7b00"), ("#d4f1e4", "#00695c"), ("#ffe0e6", "#b0003a"),
]
TYPE_LABEL = {"web": "Internet", "academic": "Publication", "encyclopedia": "Encyclopedia", "file": "Your file"}
KIND_LABEL = {"exact": "Exact match", "near": "Near-exact", "para": "Possible paraphrase"}

_FONT = None


def _fonts():
    global _FONT
    if _FONT:
        return _FONT
    cands = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
        ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ]
    for reg, bold in cands:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("Body", reg))
                pdfmetrics.registerFont(TTFont("Body-Bold", bold))
                from reportlab.pdfbase.pdfmetrics import registerFontFamily
                registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body", boldItalic="Body-Bold")
                _FONT = ("Body", "Body-Bold")
                return _FONT
            except Exception:
                continue
    _FONT = ("Helvetica", "Helvetica-Bold")
    return _FONT


def color_for(n):
    return PALETTE[(n - 1) % len(PALETTE)] if n else ("#eeeeee", "#555555")


def level_color(p):
    return "#2e7d32" if p < 15 else "#c26100" if p < 40 else "#c62828"


def _esc(s):
    return escape(s, quote=False)


def build_pdf(result: dict, doc_name: str = "Document") -> bytes:
    F, FB = _fonts()
    st = {
        "h1": ParagraphStyle("h1", fontName=FB, fontSize=20, leading=24, textColor=colors.HexColor("#111827")),
        "h2": ParagraphStyle("h2", fontName=FB, fontSize=13, leading=17, spaceBefore=10, spaceAfter=6,
                             textColor=colors.HexColor("#111827")),
        "sub": ParagraphStyle("sub", fontName=F, fontSize=9, leading=12, textColor=colors.HexColor("#6b7280")),
        "body": ParagraphStyle("body", fontName=F, fontSize=10, leading=15.5, spaceAfter=6,
                               textColor=colors.HexColor("#111827")),
        "cell": ParagraphStyle("cell", fontName=F, fontSize=8.5, leading=11),
        "cellb": ParagraphStyle("cellb", fontName=FB, fontSize=8.5, leading=11),
        "big": ParagraphStyle("big", fontName=FB, fontSize=40, leading=44, alignment=TA_CENTER),
        "bigl": ParagraphStyle("bigl", fontName=F, fontSize=9, leading=12, alignment=TA_CENTER,
                               textColor=colors.HexColor("#6b7280")),
    }
    buf = io.BytesIO()
    date = time.strftime("%d %b %Y, %H:%M")

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(F, 7.5)
        canvas.setFillColor(colors.HexColor("#9ca3af"))
        canvas.drawString(18 * mm, 10 * mm, f"OpenPlag similarity report · {doc_name[:60]} · {date}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title=f"Similarity report – {doc_name}", author="OpenPlag")
    el = []
    sim = result["similarity"]
    meta = result.get("meta", {})
    srcs = [s for s in result["sources"] if not s.get("excluded") and s["words"] > 0]

    el.append(Paragraph("Similarity Report", st["h1"]))
    el.append(Paragraph(f"{_esc(doc_name)} · generated {date}", st["sub"]))
    el.append(Spacer(1, 8))

    score = Table([[Paragraph(f'<font color="{level_color(sim)}">{sim:g}%</font>', st["big"])],
                   [Paragraph("overall similarity", st["bigl"])]], colWidths=[55 * mm])
    score.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#e5e7eb")),
                               ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
                               ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    bk = result.get("by_kind", {})
    bt = result.get("by_type", {})
    stats = [
        ("Words checked", f"{result['counted_words']:,}"),
        ("Matched words", f"{result['matched_words']:,}"),
        ("Matching sources", str(len(srcs))),
        ("Exact / near / paraphrase", f"{bk.get('exact', 0):g}% / {bk.get('near', 0):g}% / {bk.get('para', 0):g}%"),
        ("Internet / publications", f"{bt.get('web', 0) + bt.get('encyclopedia', 0):g}% / {bt.get('academic', 0):g}%"),
        ("Your files", f"{bt.get('file', 0):g}%"),
        ("Search queries", str(meta.get("queries", "–"))),
        ("Candidates / downloaded", f"{meta.get('candidates', 0)} / {meta.get('downloaded', 0)}"),
        ("Excluded (quotes / refs)", f"{result.get('quoted_words', 0)} / {result.get('reference_words', 0)} words"),
    ]
    ai = result.get("ai") or {}
    if ai.get("available"):
        stats.append(("AI-writing estimate", f"{ai['overall']:g}% (estimate – see last section)"))
    stat_tbl = Table([[Paragraph(a, st["cell"]), Paragraph(f"<b>{b}</b>", st["cell"])] for a, b in stats],
                     colWidths=[45 * mm, 70 * mm])
    stat_tbl.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#eeeeee")),
                                  ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))
    top = Table([[score, stat_tbl]], colWidths=[62 * mm, 112 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    el.append(top)
    if meta.get("providers"):
        el.append(Spacer(1, 6))
        el.append(Paragraph("Searched: " + _esc(", ".join(meta["providers"]))
                            + (" + your reference files" if meta.get("reference_files") else ""), st["sub"]))

    # ---------------------------------------------------------------- sources
    el.append(Paragraph("Matching sources", st["h2"]))
    if not srcs:
        el.append(Paragraph("No matching sources were found in the sources searched.", st["body"]))
    else:
        rows = [[Paragraph("<b>#</b>", st["cell"]), Paragraph("<b>Source</b>", st["cell"]),
                 Paragraph("<b>Type</b>", st["cell"]), Paragraph("<b>Share</b>", st["cell"])]]
        styles = [("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
                  ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                  ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
        for i, s in enumerate(srcs, 1):
            hl, strong = color_for(s["n"])
            link = (f'<br/><font color="#1f4fbf" size="7.5"><link href="{escape(s["url"])}">'
                    f'{_esc(s["url"][:110])}</link></font>') if s["url"] else ""
            rows.append([Paragraph(f'<font color="white"><b>{s["n"]}</b></font>', st["cell"]),
                         Paragraph(f'{_esc(s["title"][:160])}{link}', st["cell"]),
                         Paragraph(f'{TYPE_LABEL.get(s["type"], s["type"])}'
                                   f'{"" if s["full_text"] else "<br/><font size=7 color=#9ca3af>snippet/abstract</font>"}',
                                   st["cell"]),
                         Paragraph(f'<b>{s["pct"]:g}%</b>', st["cell"])])
            styles.append(("BACKGROUND", (0, i), (0, i), colors.HexColor(strong)))
            styles.append(("BACKGROUND", (1, i), (-1, i), colors.HexColor(hl) if i <= 3 else colors.white))
        t = Table(rows, colWidths=[9 * mm, 118 * mm, 30 * mm, 17 * mm], repeatRows=1)
        t.setStyle(TableStyle(styles))
        el.append(t)

    # ---------------------------------------------------------------- document
    el.append(PageBreak())
    el.append(Paragraph("Document with highlighted matches", st["h2"]))
    el.append(Paragraph("Coloured text matches the numbered source. Underlined colour = near-exact match, "
                        "dotted = possible paraphrase. Grey italic text was excluded (quotations / references).",
                        st["sub"]))
    el.append(Spacer(1, 6))
    paras, cur = [], []
    for seg in result["segments"]:
        parts = re.split(r"(\n+)", seg["t"])
        for part in parts:
            if not part:
                continue
            if part.startswith("\n"):
                if cur:
                    paras.append("".join(cur))
                    cur = []
                continue
            txt = _esc(part)
            c = seg.get("c")
            if c == "m":
                hl, strong = color_for(seg.get("n"))
                deco = ""
                if seg.get("k") == "near":
                    txt = f"<u>{txt}</u>"
                elif seg.get("k") == "para":
                    deco = "~"
                cur.append(f'<font backColor="{hl}">{txt}</font>'
                           f'<super><font color="{strong}" size="6"><b>{deco}{seg.get("n")}</b></font></super>')
            elif c in ("q", "r"):
                cur.append(f'<font color="#9ca3af"><i>{txt}</i></font>')
            else:
                cur.append(txt)
    if cur:
        paras.append("".join(cur))
    for p in paras:
        if p.strip():
            try:
                el.append(Paragraph(p, st["body"]))
            except Exception:
                el.append(Paragraph(_esc(re.sub(r"<[^>]+>", "", p)), st["body"]))

    # ---------------------------------------------------------------- details
    matches = sorted(result.get("matches", []), key=lambda m: -m["words"])[:80]
    if matches:
        el.append(PageBreak())
        el.append(Paragraph("Match details", st["h2"]))
        el.append(Paragraph("Longest matches first. In the source column the matching passage is in bold.",
                            st["sub"]))
        el.append(Spacer(1, 6))
        num = {s["id"]: s for s in result["sources"]}
        for m in matches:
            s = num.get(m["source"], {})
            hl, strong = color_for(m.get("n"))
            src = _esc(m["src"][:900]).replace("[[", "<b>").replace("]]", "</b>")
            if src.count("<b>") != src.count("</b>"):
                src = src.replace("<b>", "").replace("</b>", "")
            head = (f'<font color="{strong}"><b>Source {m.get("n")}</b></font> · {KIND_LABEL.get(m["kind"], m["kind"])}'
                    f' · {m["words"]} words' + (f' · {m["score"]:g}% similar' if m["kind"] != "exact" else "")
                    + f'<br/><font size="7.5" color="#6b7280">{_esc((s.get("title") or "")[:120])}</font>')
            t = Table([[Paragraph(head, st["cell"]), ""],
                       [Paragraph("<b>Your text</b><br/>" + _esc(m["doc"][:900]), st["cell"]),
                        Paragraph("<b>Source text</b><br/>" + src, st["cell"])]],
                      colWidths=[87 * mm, 87 * mm])
            t.setStyle(TableStyle([("SPAN", (0, 0), (1, 0)), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(hl)),
                                   ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
                                   ("LINEBEFORE", (1, 1), (1, 1), 0.5, colors.HexColor("#e5e7eb")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            el.append(KeepTogether([t, Spacer(1, 6)]))

    if ai.get("available"):
        el.append(PageBreak())
        el.append(Paragraph("AI-writing estimate", st["h2"]))
        lvl = ("Likely AI-generated" if ai["overall"] >= 70 else "Mixed / uncertain" if ai["overall"] >= 40
               else "Likely human-written")
        el.append(Paragraph(
            f'<font size="16" color="#7c3aed"><b>{ai["overall"]:g}%</b></font> &nbsp; <b>{lvl}</b> · '
            f'likely AI {ai["likely_ai"]:g}% · mixed {ai["mixed"]:g}% · likely human {ai["likely_human"]:g}% of words'
            f' · model: {_esc(ai.get("model", ""))}', st["body"]))
        if ai.get("comment"):
            el.append(Paragraph(_esc(ai["comment"]), st["body"]))
        el.append(Paragraph(
            "This is an estimate made by a language model, not proof. AI judges can wrongly flag formal academic "
            "writing and non-native English. Do not treat it as evidence of misconduct on its own."
            + (" The text is under 300 words, which makes the estimate especially unreliable." if ai.get("short_text")
               else ""), st["sub"]))
        el.append(Spacer(1, 6))
        body_text = "".join(sg["t"] for sg in result["segments"])
        rows = [[Paragraph("<b>AI %</b>", st["cell"]), Paragraph("<b>Passage</b>", st["cell"]),
                 Paragraph("<b>Why</b>", st["cell"])]]
        styles = [("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
                  ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                  ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
        for i, p in enumerate(ai.get("passages", []), 1):
            if p.get("ai") is None:
                continue
            col = "#7c3aed" if p["ai"] >= 70 else "#a78bfa" if p["ai"] >= 40 else "#c4b5fd"
            excerpt = re.sub(r"\s+", " ", body_text[p["start"]:p["end"]])
            excerpt = excerpt[:420] + ("…" if len(excerpt) > 420 else "")
            why = list(p.get("signals") or [])
            if p.get("ai_phrases"):
                why.append("AI-typical phrases: " + ", ".join(p["ai_phrases"][:6]))
            tcol = "white" if p["ai"] >= 40 else "#3b0764"
            rows.append([Paragraph(f'<font color="{tcol}"><b>{p["ai"]:.0f}%</b></font>', st["cell"]),
                         Paragraph(_esc(excerpt), st["cell"]),
                         Paragraph("<br/>".join("• " + _esc(w) for w in why) or "–", st["cell"])])
            styles.append(("BACKGROUND", (0, len(rows) - 1), (0, len(rows) - 1), colors.HexColor(col)))
        t = Table(rows, colWidths=[13 * mm, 95 * mm, 66 * mm], repeatRows=1)
        t.setStyle(TableStyle(styles))
        el.append(t)

    el.append(Spacer(1, 10))
    el.append(Paragraph(
        "<b>How to read this report.</b> A similarity percentage is not a verdict of plagiarism. Matches may be "
        "properly cited quotations, common phrases, or the author's own earlier work. Coverage is limited to the "
        "public web pages, open scholarly records and files that were searched; subscription journals, books and "
        "other students' private submissions are not included. Review each match before drawing conclusions.",
        st["sub"]))
    doc.build(el, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
