"""Build the two architecture documents using the retained specification's format.

Run in the document-authoring Python environment with python-docx and Pillow.
The retained v1.1 document owns styles, page geometry, table appearance and footer.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs/aws-backend-edge-server-spec-v1.1.docx"


def blocks(source: Path) -> list[tuple[str, object]]:
    result: list[tuple[str, object]] = []
    lines = source.read_text().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                    rows.append(cells)
                index += 1
            result.append(("table", rows))
            continue
        if line.startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            result.append(("code", "\n".join(code)))
            index += 1
            continue
        match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if match:
            result.append(("image", match.groups()))
        elif line.startswith("# "):
            result.append(("Title", line[2:]))
        elif line.startswith("## "):
            result.append(("Heading 1", line[3:]))
        elif line.startswith("### "):
            result.append(("Heading 2", line[4:]))
        else:
            result.append(("Normal", line))
        index += 1
    return result


def inline(paragraph, text: str) -> None:
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    for part in re.split(r"(`[^`]+`|\*\*[^*]+\*\*)", text):
        run = paragraph.add_run(part.strip("`*"))
        if part.startswith("**"):
            run.bold = True
        if part.startswith("`"):
            run.font.name = "Courier New"
            run.font.size = Pt(9)


def build(source: Path, target: Path, *, architecture: bool = False) -> None:
    doc = Document(TEMPLATE)
    template_table = deepcopy(doc.tables[0]._tbl.tblPr)
    template_header = deepcopy(doc.tables[0].rows[0].cells[0]._tc.tcPr)
    template_body = deepcopy(doc.tables[0].rows[1].cells[0]._tc.tcPr)
    header_paragraph = deepcopy(doc.tables[0].rows[0].cells[0].paragraphs[0]._p.pPr)
    body_paragraph = deepcopy(doc.tables[0].rows[1].cells[0].paragraphs[0]._p.pPr)
    widths_by_header = {
        tuple(cell.text for cell in table.rows[0].cells): [
            cell.width for cell in table.rows[0].cells
        ]
        for table in doc.tables
    }
    original_section = deepcopy(doc.sections[0]._sectPr)
    for child in list(doc._element.body):
        if child.tag != qn("w:sectPr"):
            doc._element.body.remove(child)
    section = doc.sections[0]
    width = section.page_width - section.left_margin - section.right_margin
    height = section.page_height - section.top_margin - section.bottom_margin

    def table(rows: list[list[str]]) -> None:
        count, first = len(rows[0]), rows[0][0]
        column_widths = widths_by_header.get(tuple(rows[0]))
        if column_widths is None:
            if count == 3:
                ratios = [0.14, 0.36, 0.50] if first in {"선", "관계"} else [0.26, 0.31, 0.43]
            elif count == 2:
                ratios = [0.24, 0.76]
            else:
                ratios = [1 / count] * count
            column_widths = [int(width * ratio) for ratio in ratios]
        if architecture and first == "처리 그룹":
            column_widths = [int(width * ratio) for ratio in [0.18, 0.59, 0.23]]
        if architecture and first == "기능":
            column_widths = [int(width * ratio) for ratio in [0.28, 0.44, 0.28]]
        item = doc.add_table(rows=0, cols=count)
        item._tbl.replace(item._tbl.tblPr, deepcopy(template_table))
        for column, size in zip(item.columns, column_widths, strict=True):
            column.width = size
        for row_index, values in enumerate(rows):
            row = item.add_row()
            row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
            if row_index == 0:
                row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
            for cell, value, size in zip(row.cells, values, column_widths, strict=True):
                cell._tc.replace(
                    cell._tc.tcPr, deepcopy(template_header if row_index == 0 else template_body)
                )
                cell.width = size
                paragraph = cell.paragraphs[0]
                paragraph._p.insert(
                    0, deepcopy(header_paragraph if row_index == 0 else body_paragraph)
                )
                if row_index == 1 and len(rows) > 2:
                    paragraph.paragraph_format.keep_with_next = True
                inline(paragraph, value)
                for run in paragraph.runs:
                    run.font.size = Pt(8.5)
                    run.bold = row_index == 0
        spacer = doc.add_paragraph()
        spacer.paragraph_format.line_spacing = Pt(2)
        spacer.paragraph_format.space_after = Pt(1)

    def picture(item: tuple[str, str]) -> None:
        caption, relative = item
        image_path = source.parent / relative
        with Image.open(image_path) as raster:
            image_width, image_height = raster.size
        assert width * image_height / image_width < height - Pt(45), relative
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.keep_with_next = True
        paragraph.paragraph_format.keep_together = True
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = 1
        run = paragraph.add_run()
        run.add_picture(str(image_path), width=width)
        for properties in run._element.findall(".//" + qn("wp:docPr")):
            properties.set("descr", caption)
        paragraph = doc.add_paragraph(caption)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.keep_with_next = False
        for run in paragraph.runs:
            run.font.size = Pt(8.5)

    for kind, content in blocks(source):
        if kind == "table":
            table(content)
        elif kind == "image":
            picture(content)
        elif kind == "code":
            paragraph = doc.add_paragraph()
            run = paragraph.add_run(content)
            run.font.name = "Courier New"
            run.font.size = Pt(8.5)
        else:
            paragraph = doc.add_paragraph(style=kind)
            if kind == "Heading 2" and str(content).startswith("11 1 사무실"):
                paragraph.paragraph_format.page_break_before = True
            if kind == "Title":
                content = (
                    "DSC 시스템 아키텍처\n상세 설계"
                    if architecture
                    else "퓨처렉스 DSC AI CCTV\n백엔드 및 엣지 서버 연동 규격서"
                )
            inline(paragraph, content)
    for paragraph in section.footer.paragraphs:
        for run in paragraph.runs:
            run.text = run.text.replace("Rev 1.1", "Rev 1.2")
            if architecture:
                run.text = run.text.replace("F2X DSC CCTV INT 001", "DSC 시스템 아키텍처")
    used_images = {
        node.get(qn("r:embed")) for node in doc._element.body.findall(".//" + qn("a:blip"))
    }
    for identity, relationship in list(doc.part.rels.items()):
        if relationship.reltype.endswith("/image") and identity not in used_images:
            doc.part.drop_rel(identity)
    doc.core_properties.title = (
        "DSC 시스템 아키텍처 상세 설계"
        if architecture
        else "퓨처렉스 DSC AI CCTV 백엔드 및 엣지 서버 연동 규격서"
    )
    assert len(doc.sections) == 1
    assert deepcopy(doc.sections[0]._sectPr).xml == original_section.xml
    assert all(shape.width <= width for shape in doc.inline_shapes)
    doc.save(target)
    with ZipFile(TEMPLATE) as reference, ZipFile(target) as result:
        assert reference.read("word/styles.xml") == result.read("word/styles.xml"), (
            "Template styles changed"
        )
    print(
        f"{target.name}: original A4 section and styles preserved; {len(doc.inline_shapes)} figures"
    )


if __name__ == "__main__":
    build(
        ROOT / "docs/aws-backend-edge-server-spec-v1.2.md",
        ROOT / "docs/aws-backend-edge-server-spec-v1.2.docx",
    )
    build(
        ROOT / "docs/system-architecture-v1.2.md",
        ROOT / "docs/system-architecture-v1.2.docx",
        architecture=True,
    )
