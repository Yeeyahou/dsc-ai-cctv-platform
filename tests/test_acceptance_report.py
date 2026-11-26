import ast
import json
from pathlib import Path

import pytest

from dsc.devtools.acceptance import markdown, summarize
from dsc.devtools.fault_proxy import FaultSettings


def test_report_never_promotes_skipped_missing_or_failed_checks(tmp_path):
    junit = tmp_path / "results.xml"
    junit.write_text("""<testsuites><testsuite>
      <testcase classname="tests.x" name="test_ok"/>
      <testcase classname="tests.x" name="test_pg"><skipped/></testcase>
      <testcase classname="tests.x" name="test_bad"><failure/></testcase>
    </testsuite></testsuites>""")
    matrix = [
        {
            "id": f"AT{i:02}",
            "tests": [name],
            "title": name,
            "expected": "expected",
            "fieldWork": "physical NAS",
        }
        for i, name in enumerate(["test_ok", "test_pg", "test_bad", "test_missing"], 1)
    ]
    result = summarize(junit, matrix, 0)
    assert [r["localStatus"] for r in result] == ["PASS", "INCOMPLETE", "FAIL", "INCOMPLETE"]
    assert all(r["siteStatus"] == "NOT_RUN" for r in result)
    report = markdown(
        {
            "recordedAt": "now",
            "commit": "abc",
            "boundary": "test",
            "exitCode": 0,
            "requirements": result,
        }
    )
    assert "NOT_RUN" in report and "test_missing" in report
    assert summarize(junit, matrix, 2)[0]["localStatus"] == "INCOMPLETE"
    junit.write_text("<testsuites><truncated")
    assert all(row["localStatus"] == "INCOMPLETE" for row in summarize(junit, matrix, 0))


def test_all_30_acceptance_requirements_point_to_existing_tests():
    root = Path(__file__).resolve().parents[1]
    matrix = json.loads((root / "docs/acceptance-matrix.json").read_text())
    assert [r["id"] for r in matrix] == [f"AT{i:02}" for i in range(1, 31)]
    names = {
        node.name
        for path in (root / "tests").glob("test_*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef)
    }
    for row in matrix:
        assert row["tests"] and set(row["tests"]) <= names
        assert row["expected"] and row["fieldWork"]


@pytest.mark.parametrize(
    "upstream",
    ["http://example.com", "https://user:secret@example.com", "https://example.com?token=secret"],
)
def test_fault_proxy_rejects_unsafe_upstream(upstream):
    with pytest.raises(ValueError):
        FaultSettings(upstream=upstream, mode="drop-after")
