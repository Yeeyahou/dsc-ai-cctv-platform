"""Build isolated DSC development deployment artifacts for AWS Lambda."""

import argparse
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from sam_build import build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    output = parser.parse_args().output.resolve()
    # Require a fresh path so an old build cannot be uploaded under the wrong revision.
    output.mkdir(parents=True)
    with TemporaryDirectory(prefix="dsc-lambda-build-") as tmp:
        staging = Path(tmp)
        build(staging, lambda_x86_64=True)
        # pip scripts and bytecode are unnecessary on Lambda.
        bin_dir = staging / "bin"
        if bin_dir.is_dir():
            shutil.rmtree(bin_dir)
        shutil.make_archive(str(output / "app"), "zip", root_dir=staging)
        shutil.copy2("deploy/database/dev_init.py", staging / "dev_init.py")
        shutil.copy2("deploy/database/grants.sql", staging / "grants.sql")
        shutil.copytree(
            "migrations", staging / "migrations", ignore=shutil.ignore_patterns("__pycache__")
        )
        shutil.make_archive(str(output / "db-init"), "zip", root_dir=staging)

    template = yaml.safe_load(Path("infra/template.yaml").read_text())
    for logical_id in ("ApiFunction", "MaintenanceFunction"):
        template["Resources"][logical_id]["Properties"]["CodeUri"] = "app.zip"
    (output / "app-local.yaml").write_text(yaml.safe_dump(template, sort_keys=False))


if __name__ == "__main__":
    main()
