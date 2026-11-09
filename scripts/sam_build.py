import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--lambda-x86-64", action="store_true")
    args = parser.parse_args()
    build(args.destination, lambda_x86_64=args.lambda_x86_64)


def build(destination: Path, *, lambda_x86_64: bool = False) -> None:
    target = (
        [
            "--only-binary=:all:",
            "--platform",
            "manylinux2014_x86_64",
            "--platform",
            "manylinux_2_28_x86_64",
            "--platform",
            "manylinux_2_34_x86_64",
            "--python-version",
            "3.12",
            "--implementation",
            "cp",
            "--abi",
            "cp312",
        ]
        if lambda_x86_64
        else []
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--no-compile",
            *target,
            "-r",
            "requirements.lock",
            "-t",
            str(destination),
        ],
        check=True,
    )
    shutil.copytree(
        "src/dsc",
        destination / "dsc",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


if __name__ == "__main__":
    main()
