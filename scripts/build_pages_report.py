#!/usr/bin/env python3
"""Build the deterministic public site from validated structured artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.site.builder import build_site  # noqa: E402


def build_pages(*, output_dir: Path | None = None) -> list[Path]:
    return build_site(root=ROOT_DIR, output_dir=output_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build Pages in a fresh staging directory and promote the validated tree transactionally."
    )
    parser.add_argument("--output-dir", type=Path, help="Override the final site output directory")
    args = parser.parse_args(argv)
    files = build_pages(output_dir=args.output_dir)
    print(f"Built deterministic site with {len(files)} files: {args.output_dir or ROOT_DIR / 'site'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
