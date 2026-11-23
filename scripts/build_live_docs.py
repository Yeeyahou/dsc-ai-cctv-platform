"""Build the v1.3 specifications and installation documents, retaining A4 template styles."""

from build_architecture_docs import ROOT, build

SOURCES = [
    ("aws-backend-edge-server-spec-v1.3", None),
    ("system-architecture-v1.3", "DSC 시스템 아키텍처와 실시간 시청 설계"),
    ("streaming-installation-guide", "DSC 실시간 CCTV 설치와 운영 안내"),
    ("installation-readiness", "DSC 설치 전 준비와 당일 인수 절차"),
    ("live-api-v1.3", "DSC 실시간 시청 API 명세"),
    ("acceptance-report-guide", "DSC 검수 보고서 생성과 인수 안내"),
    ("acceptance-report-template", "DSC 실제 환경 검수 보고서 양식"),
]


if __name__ == "__main__":
    for stem, title in SOURCES:
        build(
            ROOT / f"docs/{stem}.md",
            ROOT / f"docs/{stem}.docx",
            document_title=title,
            architecture=stem == "system-architecture-v1.3",
            revision="1.3",
            keep_code_together=True,
            page_break_before={
                "streaming-installation-guide": ("검수와 참고 문서",),
                "acceptance-report-template": ("v1.3 실시간 시청 추가 인수",),
            }.get(stem, ()),
        )
