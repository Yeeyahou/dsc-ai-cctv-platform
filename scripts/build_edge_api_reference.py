"""Render the authored Markdown using the retained specification's A4 Word format.

Requires the document-authoring environment with python-docx and Pillow.
Does not regenerate or edit the retained specification or architecture documents.
"""

from __future__ import annotations

from copy import deepcopy
from zipfile import ZipFile

from build_architecture_docs import ROOT, TEMPLATE, build
from docx import Document
from docx.shared import Pt


def main() -> None:
    source = ROOT / "docs/edge-cloud-api-v1.2.md"
    target = source.with_suffix(".docx")
    titles = ("5 API 상세",) + tuple(
        line[4:]
        for line in source.read_text().splitlines()
        if line.startswith("### 5 ") and not line.startswith("### 5 1 EC01 ")
    )
    # The retained source owns section, styles, numbering, header and footer geometry.
    build(
        source,
        target,
        document_title="현장 엣지 및 사무실 워커\nAWS API 명세서",
        page_break_before=titles,
    )
    reference = Document(TEMPLATE)
    doc = Document(target)
    section = doc.sections[0]
    width = section.page_width - section.left_margin - section.right_margin
    captions = {
        "요청 필드",
        "요청 본문 예제",
        "응답 필드",
        "추가 업무 오류",
        "처리 규칙",
        "cURL 호출 예제",
        "200 응답 예제",
        "201 응답 예제",
        "동일한 최초 이벤트 재전송의 200 응답 예제",
        "AssetLink",
        "Media",
    }
    in_rules = False
    for paragraph in doc.paragraphs:
        if paragraph.text == "처리 규칙":
            in_rules = True
        elif paragraph.text == "cURL 호출 예제":
            in_rules = False
        elif in_rules and paragraph.text[:1].isdigit():
            paragraph.paragraph_format.keep_with_next = True
        if paragraph.runs and all(run.font.name == "Courier New" for run in paragraph.runs):
            paragraph.paragraph_format.keep_together = True
            paragraph.paragraph_format.line_spacing = 1.0
            paragraph.paragraph_format.space_before = Pt(3)
            paragraph.paragraph_format.space_after = Pt(5)
        if paragraph.text in captions:
            paragraph.paragraph_format.keep_with_next = True
            paragraph.paragraph_format.space_before = Pt(6)
            for run in paragraph.runs:
                run.bold = True
        for run in paragraph.runs:
            if run.font.name == "Courier New":
                run.font.size = Pt(9)
    for table in doc.tables:
        headers = [cell.text for cell in table.rows[0].cells]
        if headers == ["항목", "내용"] and table.rows[1].cells[0].text == "서버":
            table._tbl.remove(table.rows[0]._tr)
        if len(headers) == 4:
            ratios = [0.28, 0.15, 0.08, 0.49]
        elif len(headers) == 3:
            ratios = (
                [0.09, 0.63, 0.28]
                if headers[0] == "ID"
                else [0.10, 0.37, 0.53]
                if headers[0] == "HTTP"
                else [0.28, 0.27, 0.45]
            )
        else:
            ratios = [0.22, 0.78]
        table.autofit = False
        for column, ratio in zip(table.columns, ratios, strict=True):
            column.width = int(width * ratio)
        for index, row in enumerate(table.rows):
            for cell, ratio in zip(row.cells, ratios, strict=True):
                cell.width = int(width * ratio)
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.keep_with_next = index == 0
                    for run in paragraph.runs:
                        run.font.size = Pt(9)
    for paragraph in section.footer.paragraphs:
        for run in paragraph.runs:
            run.text = run.text.replace("DSC 연동 준비", "DSC EDGE CLOUD API")
    doc.core_properties.subject = "현장 엣지 및 사무실 워커의 AWS 제어 API 13개"
    doc.core_properties.title = "현장 엣지 및 사무실 워커 AWS API 명세서"
    assert len(doc.sections) == 1
    assert deepcopy(section._sectPr).xml == deepcopy(reference.sections[0]._sectPr).xml
    doc.save(target)
    with ZipFile(TEMPLATE) as original, ZipFile(target) as result:
        for part in ("word/styles.xml", "word/numbering.xml", "word/theme/theme1.xml"):
            assert original.read(part) == result.read(part), part
    print(f"{target.name}: A4, styles, numbering and theme preserved")


if __name__ == "__main__":
    main()
