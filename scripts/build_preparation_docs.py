"""Build only the six preparation guides using the retained A4 specification styles."""

from build_architecture_docs import ROOT, build

SOURCES = [
    ("producer-integration-contract", "분석 프로그램과 현장 엣지 연동 계약"),
    ("producer-mock-guide", "분석 및 녹화 목업 실행 안내"),
    ("aas-consumer-guide", "외부 AAS 소비자 목업 실행 안내"),
    ("fault-rehearsal-guide", "장애 재현 도구와 무결성 확인 안내"),
    ("acceptance-report-guide", "검수 보고서 생성과 인수 안내"),
    ("acceptance-report-template", "실제 환경 검수 보고서 양식"),
]


if __name__ == "__main__":
    for stem, title in SOURCES:
        breaks = {
            "producer-mock-guide": ("확인과 한계",),
            "acceptance-report-guide": ("실제 환경 기록",),
        }
        build(
            ROOT / f"docs/{stem}.md",
            ROOT / f"docs/{stem}.docx",
            document_title=title,
            page_break_before=breaks.get(stem, ()),
        )
