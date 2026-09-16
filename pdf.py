"""Markdown → PDF for downloads.

Case files stay Markdown in Drive (the AI reads them); they are converted to PDF only when offered for download.
Uses the bundled DejaVu Sans font so non-Latin-1 text (°, –, ≥, µ …) prints the same locally and on Streamlit Cloud.
"""
from __future__ import annotations

import re

import markdown
from fpdf import FPDF, TextStyle
from fpdf.html import HTML2FPDF

import config

FONTS_DIR = config.BASE_DIR / "fonts"
FONT = "DejaVu"
TEXT_COLOR = (33, 37, 41)
MUTED_COLOR = (108, 117, 125)
LINE_HEIGHT = "1.35"
BODY_SIZE = 10

_LIST_ITEM = re.compile(r"^(\s*)(?:[*+-]|\d+[.)])\s+")


class _HTML(HTML2FPDF):
    def handle_starttag(self, tag, attrs):
        if tag == "li":
            # List markers are drawn with the PDF's current font, which may still be the previous heading's.
            self.pdf.set_font(FONT, size=BODY_SIZE)
        super().handle_starttag(tag, attrs)


class _Document(FPDF):
    HTML2FPDF_CLASS = _HTML

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(FONT, size=8)
        self.set_text_color(*MUTED_COLOR)
        self.cell(0, 6, f"Page {self.page_no()} of {{nb}}", align="C")


def _normalize_markdown(text: str) -> str:
    """Adapt AI-style Markdown to Python-Markdown's stricter rules.

    Nested list items indented by 2 or 3 spaces are re-indented by 4 per level, and a blank line is
    inserted before a list or table that directly follows a paragraph (otherwise both render as plain text).
    """
    out: list[str] = []
    indents: list[int] = []  # source indent of each open list level
    block = None  # "list" | "table" | None: what the previous non-blank line belonged to
    top_ordered = False  # whether the current top-level list is numbered
    for line in text.splitlines():
        item = _LIST_ITEM.match(line)
        if item:
            indent = len(item.group(1).expandtabs(4))
            while indents and indent < indents[-1]:
                indents.pop()
            if not indents or indent > indents[-1]:
                indents.append(indent)
            ordered = item.group(0).strip()[0].isdigit()
            new_list = block != "list" or (len(indents) == 1 and ordered != top_ordered)  # sane_lists splits ul/ol
            if new_list and out and out[-1].strip():
                out.append("")
            if len(indents) == 1:
                top_ordered = ordered
            out.append("    " * (len(indents) - 1) + line.lstrip())
            block = "list"
        elif line.lstrip().startswith("|"):
            if block != "table" and out and out[-1].strip():
                out.append("")
            out.append(line.strip())
            block = "table"
        else:
            if line.strip() and not (block == "list" and line[:1].isspace()):
                indents, block = [], None
            out.append(line)
    return "\n".join(out)


def _html(text: str) -> str:
    html = markdown.markdown(_normalize_markdown(text), extensions=["tables", "sane_lists"])
    html = re.sub(r"<(p|ul|ol|blockquote)>", rf'<\1 line-height="{LINE_HEIGHT}">', html)
    html = re.sub(r"(</(?:p|blockquote)>\n)<table>", r"\1<br><table>", html)  # otherwise the table touches the text above
    return re.sub(r"<(td|th)(?: style=\"text-align: (\w+);\")?>", lambda m: f'<{m.group(1)} align="{m.group(2) or "left"}">', html)


def markdown_to_pdf(text: str, title: str) -> bytes:
    """Render a Markdown document (headings, lists, tables, quotes, bold) as an A4 PDF."""
    html = _html(text)

    pdf = _Document(format="A4")
    pdf.set_title(title)
    pdf.set_creator("NutritionPlan")
    # Only regular and bold faces are bundled; italic text is printed upright.
    pdf.add_font(FONT, "", FONTS_DIR / "DejaVuSans.ttf")
    pdf.add_font(FONT, "B", FONTS_DIR / "DejaVuSans-Bold.ttf")
    pdf.add_font(FONT, "I", FONTS_DIR / "DejaVuSans.ttf")
    pdf.add_font(FONT, "BI", FONTS_DIR / "DejaVuSans-Bold.ttf")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font(FONT, size=BODY_SIZE)
    pdf.set_text_color(*TEXT_COLOR)

    heading = lambda size, top: TextStyle(FONT, "B", size, TEXT_COLOR, t_margin=top, b_margin=0.3)
    pdf.write_html(
        html,
        font_family=FONT,
        pre_code_font=FONT,
        li_prefix_color=TEXT_COLOR,
        table_line_separators=True,
        tag_styles={
            "h1": heading(17, 0),
            "h2": heading(13.5, 6),
            "h3": heading(11.5, 4),
            "h4": heading(10.5, 3),
            "blockquote": TextStyle(FONT, "", 10, MUTED_COLOR, l_margin=8, t_margin=2, b_margin=4),
        },
    )
    return bytes(pdf.output())
