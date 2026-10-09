"""Compare a DockerGo wheel or installation with the checkout's Python files."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path


class VerificationError(Exception):
    pass


def _source_files(source_root: Path):
    package_root = source_root / "dockergo"
    if not package_root.is_dir():
        raise VerificationError(f"package directory not found: {package_root}")
    return package_root, sorted(
        path for path in package_root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def verify_wheel(source_root: Path, wheel_path: Path) -> int:
    package_root, source_files = _source_files(source_root)
    try:
        with zipfile.ZipFile(wheel_path) as wheel:
            for source_file in source_files:
                relative = source_file.relative_to(package_root).as_posix()
                wheel_name = f"dockergo/{relative}"
                try:
                    packaged = wheel.read(wheel_name)
                except KeyError as exc:
                    raise VerificationError(f"wheel is missing {wheel_name}") from exc
                if packaged != source_file.read_bytes():
                    raise VerificationError(f"wheel module differs from source: {wheel_name}")
    except zipfile.BadZipFile as exc:
        raise VerificationError(f"invalid wheel: {wheel_path}") from exc
    return len(source_files)


def verify_installed(source_root: Path, package_dir: Path) -> int:
    package_root, source_files = _source_files(source_root)
    for source_file in source_files:
        relative = source_file.relative_to(package_root)
        installed_file = package_dir / relative
        if not installed_file.is_file():
            raise VerificationError(f"installed package is missing {installed_file}")
        if source_file.read_bytes() != installed_file.read_bytes():
            raise VerificationError(f"installed module differs from source: {installed_file}")
    return len(source_files)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("wheel", "installed"))
    parser.add_argument("source_root", type=Path)
    parser.add_argument("target", type=Path,
                        help="wheel file or installed dockergo package directory")
    args = parser.parse_args(argv)
    try:
        count = (verify_wheel(args.source_root, args.target) if args.mode == "wheel"
                 else verify_installed(args.source_root, args.target))
    except (OSError, VerificationError) as exc:
        print(f"package verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified {count} Python modules ({args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())