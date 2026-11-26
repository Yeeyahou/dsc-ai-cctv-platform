import argparse
import json
from pathlib import Path

import pytest

from dsc.devtools.preflight import nas_probe, run


def test_nas_probe_preserves_existing_data_and_checks_marker(tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    (root / ".dsc-volume-id").write_text("test-volume")
    (root / "original").write_bytes(b"must-survive")
    nas_probe(root, "test-volume")
    assert sorted(path.name for path in root.iterdir()) == [".dsc-volume-id", "original"]
    assert (root / "original").read_bytes() == b"must-survive"
    with pytest.raises(ValueError):
        nas_probe(root, "wrong-volume")
    link = tmp_path / "link"
    link.symlink_to(root)
    with pytest.raises(ValueError):
        nas_probe(link, "test-volume")


def test_preflight_reports_types_without_secret_paths_or_values(tmp_path):
    path = tmp_path / "private-secret-env"
    path.write_text("password=private-value")
    path.chmod(0o644)
    args = argparse.Namespace(
        field_env=path,
        office_env=None,
        live_env=None,
        installation=None,
        network=False,
        nas_root=None,
        volume_id="",
    )
    report = run(args)
    assert report == [
        {"check": "field-configuration", "status": "FAIL", "errorType": "PermissionError"}
    ]
    assert str(path) not in json.dumps(report)
    assert "private-value" not in json.dumps(report)


def test_bundle_only_reads_approved_source_files():
    from scripts.build_install_bundle import selected

    paths = selected()
    assert paths
    assert all("runtime" not in path.parts and "__pycache__" not in path.parts for path in paths)
    assert not any(path.suffix in {".mp4", ".sqlite"} for path in paths)
    assert not any(path.name == "aws-backend-edge-server-spec-v1.2.docx" for path in paths)
    assert Path("src/dsc/streaming/web/app.js").resolve() in paths
