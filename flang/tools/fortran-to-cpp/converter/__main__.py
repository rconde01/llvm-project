"""``python -m converter`` — CLI front-end for the translator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from flang_ast import FlangError

from . import convert_ast, convert_file, convert_files


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fortran-to-cpp",
        description=(
            "Translate Fortran source (via flang's JSON AST dump) into C++ "
            "that uses the fortran-to-cpp runtime.  Pass several files to "
            "convert a multi-file project, resolving inter-module USE deps."
        ),
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("source", nargs="*", default=[], help="Fortran source file(s).")
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
        help="Write to this file instead of stdout (single source only).",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        help="With multiple sources, write each to <stem>.cpp in this directory.",
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
    try:
        if args.ast:
            cpp = convert_ast(
                args.ast,
                source_file=str(args.source_file) if args.source_file else None,
            )
        elif len(args.source) > 1:
            return _convert_project(args)
        elif args.source:
            cpp = convert_file(
                args.source[0], flang=args.flang, sema=not args.no_sema
            )
        else:
            _build_parser().error("no source file given")
            return 2
    except FlangError as exc:
        # flang couldn't parse/analyze the input (bad encoding, a sema
        # error, ...).  Report cleanly rather than dumping a traceback.
        detail = (exc.stderr or str(exc)).strip().splitlines()
        print(f"fortran-to-cpp: {detail[0] if detail else exc}", file=sys.stderr)
        return 1
    if args.output:
        args.output.write_text(cpp, encoding="utf-8")
    else:
        sys.stdout.write(cpp)
    return 0


def _convert_project(args: argparse.Namespace) -> int:
    results = convert_files(args.source, flang=args.flang)
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for src, text in results.items():
            # The shared header keeps its name; sources become <stem>.cpp.
            out_name = src.name if src.suffix in (".hpp", ".h") else src.stem + ".cpp"
            (args.out_dir / out_name).write_text(text, encoding="utf-8")
    else:
        for src, text in results.items():
            sys.stdout.write(f"// ===== {src} =====\n{text}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
