"""Fortran-to-C++ source-to-source translator.

Pipeline:

  Fortran  →  flang JSON AST  →  ``flang_ast.Node`` tree  →
  comment annotation (``flang_ast.annotate``)  →  IR
  (``converter.ir``)  →  C++ source (``converter.emit``).

Public entry point :func:`convert_file` runs the whole pipeline; see
``__main__.py`` for the CLI front-end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from flang_ast import (
    annotate_tree,
    parse_fortran_file,
    parse_json_file,
)

from .emit import emit_translation_unit
from .ir import IRTranslationUnit
from .lowering import lower_program


def convert_file(
    source: str | Path,
    *,
    flang: str | None = None,
    sema: bool = True,
) -> str:
    """Translate ``source`` (a Fortran file) and return the C++ text."""
    root = parse_fortran_file(source, flang=flang, sema=sema)
    annotate_tree(root)
    tu = lower_program(root, source_file=str(source))
    return emit_translation_unit(tu)


def convert_ast(
    ast_json: str | Path,
    *,
    source_file: str | None = None,
) -> str:
    """Translate a pre-computed AST JSON file and return the C++ text."""
    root = parse_json_file(ast_json)
    annotate_tree(root)
    tu = lower_program(root, source_file=source_file)
    return emit_translation_unit(tu)


def lower_to_ir(source: str | Path, *, flang: str | None = None) -> IRTranslationUnit:
    """Run only the lowering pass — useful for tests / debugging."""
    root = parse_fortran_file(source, flang=flang)
    annotate_tree(root)
    return lower_program(root, source_file=str(source))


__all__ = [
    "IRTranslationUnit",
    "convert_ast",
    "convert_file",
    "lower_to_ir",
]
