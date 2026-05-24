"""``python -m converter`` — CLI front-end for the translator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import convert_ast, convert_file


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fortran-to-cpp",
        description=(
            "Translate a Fortran source file (via flang's JSON AST dump) "
            "into C++ that uses the fortran-to-cpp runtime."
        ),
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("source", nargs="?", help="Fortran source file.")
    src.add_argument(
        "--ast",
        type=Path,
        help="Pre-computed AST JSON (from -fdebug-dump-parse-tree-json).",
    )
    p.add_argument(
        "--source-file",
        type=Path,
        help="When using --ast, the original Fortran file (recorded in the header).",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write to this file instead of stdout.",
    )
    p.add_argument("--flang", help="Override the flang binary path.")
    p.add_argument(
        "--no-sema",
        action="store_true",
        help="Use -fdebug-dump-parse-tree-json-no-sema when invoking flang.",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.source:
        cpp = convert_file(args.source, flang=args.flang, sema=not args.no_sema)
    else:
        cpp = convert_ast(
            args.ast,
            source_file=str(args.source_file) if args.source_file else None,
        )
    if args.output:
        args.output.write_text(cpp, encoding="utf-8")
    else:
        sys.stdout.write(cpp)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
