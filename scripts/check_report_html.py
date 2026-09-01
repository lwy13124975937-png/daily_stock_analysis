#!/usr/bin/env python3
"""Validate the complete public site against its build manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.site.validator import validate_site  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a deterministic Pages artifact")
    parser.add_argument("--site-dir", type=Path, default=ROOT_DIR / "site")
    args = parser.parse_args(argv)
    errors = validate_site(args.site_dir)
    if errors:
        print("ERROR: public site contract validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Public site contract passed: {args.site_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
