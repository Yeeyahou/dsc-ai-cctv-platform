import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            "requirements.lock",
            "-t",
            str(args.destination),
        ],
        check=True,
    )
    shutil.copytree(
        "src/dsc",
        args.destination / "dsc",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


if __name__ == "__main__":
    main()
