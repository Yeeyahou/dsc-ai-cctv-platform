"""Run the isolated regression suite and build honest AT01–AT25 evidence reports."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from dsc.devtools.producer import publish
from dsc.shared.observability import configure_logging, logged


def summarize(junit: Path, matrix: list[dict], return_code: int) -> list[dict]:
    try:
        cases = list(ET.parse(junit).iter("testcase"))
    except (ET.ParseError, FileNotFoundError):
        cases = []
    results = []
    for requirement in matrix:
        matched = [
            case
            for case in cases
            if any(case.get("name", "").split("[", 1)[0] == name for name in requirement["tests"])
        ]
        found = {case.get("name", "").split("[", 1)[0] for case in matched}
        missing = sorted(set(requirement["tests"]) - found)
        failed = any(
            case.find("failure") is not None or case.find("error") is not None for case in matched
        )
        skipped = any(case.find("skipped") is not None for case in matched)
        status = "FAIL" if failed else "INCOMPLETE" if missing or skipped or return_code else "PASS"
        results.append(
            {
                **requirement,
                "localStatus": status,
                "siteStatus": "NOT_RUN",
                "caseCount": len(matched),
                "skipped": sum(case.find("skipped") is not None for case in matched),
                "missingTests": missing,
                "evidence": [f"{case.get('classname')}.{case.get('name')}" for case in matched],
            }
        )
    return results


def markdown(report: dict) -> str:
    lines = [
        "# 시스템 검증 실행 보고서",
        "",
        f"기록 시각: {report['recordedAt']}",
        f"소스 커밋: `{report['commit']}`",
        f"시험 경계: {report['boundary']}",
        f"실행 종료 코드: {report['exitCode']}",
        "",
        "PASS는 해당 자동 시험만 통과했다는 뜻이다. 실제 AWS·NAS·현장·파트너 검수 상태는 "
        "모두 NOT_RUN이며 현장 시험 기록과 함께 판정한다.",
        "",
        "| 항목 | 자동 시험 | 실행 수 | 건너뜀 | 실제 환경 |",
        "|---|---|---|---|---|",
    ]
    for item in report["requirements"]:
        lines.append(
            f"| {item['id']} {item['title']} | {item['localStatus']} | "
            f"{item['caseCount']} | {item['skipped']} | {item['siteStatus']} |"
        )
    lines += ["", "## 항목별 기대 결과와 남은 검수", ""]
    for item in report["requirements"]:
        lines += [
            f"### {item['id']} {item['title']}",
            "",
            f"기대 결과: {item['expected']}",
            f"자동 시험 범위: {item.get('localScope', '지정한 자동 시험만 확인')}",
            f"실제 환경에서 확인할 내용: {item['fieldWork']}",
            f"누락 시험: {', '.join(item['missingTests']) or '없음'}",
            "",
            "자동 시험 증거: " + ", ".join(f"`{value}`" for value in item["evidence"]),
            "",
        ]
    return "\n".join(lines) + "\n"


@logged()
def run(output: Path, root: Path, *, postgres: bool = False, timeout: int = 600) -> int:
    # Exclusive output directory prevents accidentally reporting results from an older run.
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    if postgres:
        if not env.get("DSC_TEST_POSTGRES_URL"):
            raise ValueError("--postgres requires an explicitly selected disposable database")
    else:
        env.pop("DSC_TEST_POSTGRES_URL", None)
    env["PYTHONPATH"] = str(root / "src")
    junit = output.resolve() / "junit.xml"
    command = [sys.executable, "-m", "pytest", "tests", f"--junitxml={junit}"]
    code = 2
    with (output / "pytest.log").open("w") as log:
        try:
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            code = result.returncode
        except subprocess.TimeoutExpired:
            log.write("\nRehearsal timed out. Results are incomplete.\n")
            code = 124
    matrix = json.loads((root / "docs/acceptance-matrix.json").read_text())
    if junit.exists():
        requirements = summarize(junit, matrix, code)
    else:
        requirements = [
            {
                **item,
                "localStatus": "INCOMPLETE",
                "siteStatus": "NOT_RUN",
                "caseCount": 0,
                "skipped": 0,
                "missingTests": item["tests"],
                "evidence": [],
            }
            for item in matrix
        ]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    report = {
        "recordedAt": datetime.now(UTC).isoformat(),
        "commit": commit,
        "trackedChanges": bool(dirty.stdout.strip()),
        "python": platform.python_version(),
        "os": platform.system(),
        "boundary": "isolated-python-and-loopback-processes",
        "database": "disposable-postgresql" if postgres else "temporary-sqlite",
        "exitCode": code,
        "requirements": requirements,
    }
    publish(
        output / "report.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    )
    publish(output / "report.md", markdown(report).encode())
    print(
        json.dumps(
            {
                "exitCode": code,
                "requirements": len(requirements),
                "incomplete": sum(r["localStatus"] != "PASS" for r in requirements),
            }
        )
    )
    return (
        code if code else (1 if any(r["localStatus"] == "INCOMPLETE" for r in requirements) else 0)
    )


def main() -> int:
    configure_logging("acceptance")
    parser = argparse.ArgumentParser(description="AT01–AT25 isolated acceptance evidence runner")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="Use DSC_TEST_POSTGRES_URL; tests DELETE tables, disposable DB only",
    )
    args = parser.parse_args()
    return run(args.output, args.root.resolve(), postgres=args.postgres)


if __name__ == "__main__":
    sys.exit(main())
