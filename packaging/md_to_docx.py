#!/usr/bin/env python3
"""Regenerate the Word (.docx) versions of the Markdown docs.

    pip install pypandoc_binary python-docx        # once, any venv
    python packaging/md_to_docx.py                 # all three main docs
    python packaging/md_to_docx.py docs/USER_MANUAL.md

The .md files stay the source; each .docx is written next to its .md
(A4, justified body text, bordered tables, diagram scaled to the page,
author Meena Salika).
"""
import sys, pathlib, pypandoc
from docx import Document
from docx.shared import Inches, Pt, Mm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

def borders(tbl):
    tblPr = tbl._tbl.tblPr
    b = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "single"); e.set(qn("w:sz"), "4"); e.set(qn("w:color"), "A6A6A6")
        b.append(e)
    tblPr.append(b)

def shade(cell, fill="E7ECF2"):
    tcPr = cell._tc.get_or_add_tcPr()
    s = OxmlElement("w:shd"); s.set(qn("w:val"), "clear"); s.set(qn("w:fill"), fill); tcPr.append(s)

def convert(md: pathlib.Path):
    out = md.with_suffix(".docx")
    text = md.read_text()
    for n in ("USER_MANUAL", "PROJECT_DOCUMENTATION", "INSTALLATION"):
        text = text.replace(f"]({n}.md)", f"]({n}.docx)")      # Word links point at Word files
    pypandoc.convert_text(text, "docx", format="gfm+task_lists", outputfile=str(out),
                          extra_args=[f"--resource-path={md.parent}"])
    doc = Document(str(out))
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = sec.top_margin = sec.bottom_margin = Mm(20)
    usable = sec.page_width - sec.left_margin - sec.right_margin
    for t in doc.tables:
        borders(t)
        hdr = t.rows[0]
        trPr = hdr._tr.get_or_add_trPr(); rh = OxmlElement("w:tblHeader"); rh.set(qn("w:val"), "true"); trPr.append(rh)
        for c in hdr.cells:
            shade(c)
            for p in c.paragraphs:
                for r in p.runs: r.bold = True
        for row in t.rows:
            for c in row.cells:
                for p in c.paragraphs:
                    for r in p.runs: r.font.size = Pt(9.5)
    # Justify body text and list items; keep headings, code, figures and table cells as they are.
    keep = ("Heading", "Title", "Subtitle", "Source Code", "Figure", "Captioned Figure", "Image Caption")
    for para in doc.paragraphs:                       # top-level paragraphs only (not table cells)
        if para.style.name.startswith(keep) or not para.text.strip():
            continue
        if para._p.xpath(".//w:drawing"):
            continue
        para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    for shp in doc.inline_shapes:
        if shp.width > usable:
            ratio = usable / shp.width
            shp.width = int(shp.width * ratio); shp.height = int(shp.height * ratio)
    doc.core_properties.author = "Meena Salika"
    doc.core_properties.last_modified_by = "Meena Salika"
    doc.core_properties.title = md.read_text().splitlines()[0].lstrip("# ").strip()
    doc.save(str(out))
    print(out, out.stat().st_size, "bytes,", len(doc.tables), "tables,", len(doc.inline_shapes), "images")

DEFAULT = ("INSTALLATION.md", "USER_MANUAL.md", "PROJECT_DOCUMENTATION.md")
DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
for a in (sys.argv[1:] or [DOCS / d for d in DEFAULT]):
    convert(pathlib.Path(a))
