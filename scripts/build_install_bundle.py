"""Package explicitly selected source, examples and operating documents; never local secrets."""

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = [
    "src",
    "migrations",
    "deploy/edge",
    "deploy/streaming",
    "deploy/database",
    "examples",
    "infra",
]
FILES = [
    "README.md",
    "pyproject.toml",
    "requirements.lock",
    "requirements-dev.lock",
    "alembic.ini",
    "Makefile",
    "compose.yaml",
    "scripts/build_install_bundle.py",
    "scripts/check_field_docker.py",
    "scripts/check_streaming_browser.cjs",
    "scripts/check_streaming.py",
    "docs/aws-backend-edge-server-spec-v1.3.md",
    "docs/aws-backend-edge-server-spec-v1.3.docx",
    "docs/system-architecture-v1.3.md",
    "docs/system-architecture-v1.3.docx",
    "docs/streaming-installation-guide.md",
    "docs/streaming-installation-guide.docx",
    "docs/installation-readiness.md",
    "docs/installation-readiness.docx",
    "docs/live-api-v1.3.md",
    "docs/live-api-v1.3.docx",
    "docs/office-worker-operations.md",
    "docs/field-docker-guide.md",
    "docs/producer-integration-contract.md",
    "docs/producer-mock-guide.md",
    "docs/fault-rehearsal-guide.md",
    "docs/aas-consumer-guide.md",
    "docs/openapi.json",
    "docs/deployment.md",
    "docs/dev-deployment.md",
    "docs/implementation-decisions.md",
    "docs/logging.md",
    "docs/verification.md",
    "docs/branching.md",
    "docs/preparation-work-guide.md",
    "docs/acceptance-report-guide.md",
    "docs/acceptance-report-guide.docx",
    "docs/acceptance-report-template.md",
    "docs/acceptance-report-template.docx",
    "docs/aws-backend-edge-server-spec-v1.1.docx",
    "docs/acceptance-matrix.json",
]


def selected() -> list[Path]:
    files = {ROOT / name for name in FILES}
    tracked = (
        subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
        .stdout.decode()
        .split("\0")
    )
    for name in tracked:
        path = ROOT / name
        if name and (
            any(name.startswith(folder + "/") for folder in DIRECTORIES)
            or name.startswith("scripts/")
            or name.startswith("tests/")
            or name.startswith("docs/")
            and path.suffix in {".md", ".png", ".svg", ".json"}
            or name == ".dockerignore"
        ):
            files.add(path)
    files.update((ROOT / "src/dsc/streaming").glob("*.py"))
    files.update((ROOT / "src/dsc/streaming/web").glob("*"))
    files.update(
        ROOT / name
        for name in [
            "src/dsc/backend/live.py",
            *[
                f"examples/live-api/la0{i}-{kind}.json"
                for i in range(1, 6)
                for kind in (["response"] if i == 1 else ["request", "response"])
            ],
            "src/dsc/devtools/preflight.py",
            "migrations/versions/b731a6f209cd_live_viewing.py",
            "tests/test_live.py",
            "tests/test_streaming_gateway.py",
            "tests/test_preflight.py",
            "tests/support/live_rehearsal.py",
            "examples/live-registration.json",
            "deploy/streaming/Dockerfile",
            "deploy/streaming/compose.yaml",
            "deploy/streaming/compose.env.example",
            "deploy/streaming/live.env.example",
            "deploy/streaming/installation.example.json",
            "deploy/streaming/camera-credentials.example.json",
            "deploy/streaming/nginx.conf.example",
            "scripts/build_live_docs.py",
            "scripts/render_streaming_architecture.py",
            "docs/diagrams/live-media-v1.3.svg",
            "docs/diagrams/live-media-v1.3.png",
            "docs/diagrams/live-control-v1.3.svg",
            "docs/diagrams/live-control-v1.3.png",
        ]
    )
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing approved bundle input: {path.relative_to(ROOT)}")
    return sorted(files)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {}
    with tarfile.open(args.output / "dsc-install-1.3.0.tar.gz", "w:gz") as archive:
        for path in selected():
            name = str(path.relative_to(ROOT))
            manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            archive.add(path, arcname=f"dsc-install/{name}", recursive=False)
    (args.output / "SHA256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.output / "README.txt").write_text(
        "DSC 1.3 installation sources and examples. Start with docs/installation-readiness.md.\n"
        "No credentials, media, runtime DB or Docker image archives are included.\n"
        "Physical acceptance is NOT_RUN. Build images for the target CPU before installation.\n"
    )
    print(f"packaged {len(manifest)} approved files")


if __name__ == "__main__":
    main()
