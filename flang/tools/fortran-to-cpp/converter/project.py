"""Multi-file conversion: resolve inter-module dependencies, then convert.

Modern Fortran spreads ``module`` definitions across files and wires them
together with ``USE``.  Semantic analysis of a file that ``USE``s a module
needs that module's compiled ``.mod`` file, which only exists once the
*defining* file has been processed.  Converting such a project one file at
a time (as :func:`converter.convert_file` does) therefore fails on the
dependents.

This module resolves the file-level dependency order from the ``module`` /
``USE`` graph, then processes the files in that order through a single
shared module directory.  Because dumping a file's parse tree also writes
its ``.mod``, each file's modules become available to the files that come
after it — no separate compilation step is required.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable

from flang_ast import annotate_tree, parse_fortran_file
from flang_ast.nodes import Node

from .emit import emit_translation_unit
from .lowering import lower_program
from .state_plumbing import plumb_state


def convert_files(
    sources: Iterable[str | Path], *, flang: str | None = None
) -> dict[Path, str]:
    """Translate several Fortran files, honoring inter-module ``USE`` deps.

    Returns a mapping from each input path to its generated C++ text.
    Files are parsed in dependency order through a shared module
    directory so that a file may ``USE`` modules defined in another.
    """
    paths = [Path(s) for s in sources]
    order = _dependency_order(paths, flang=flang)
    results: dict[Path, str] = {}
    with tempfile.TemporaryDirectory(prefix="f2cpp-mods-") as moddir:
        for src in order:
            extra = ["-I", moddir]
            if _needs_cpp(src):
                extra.append("-cpp")
            root = parse_fortran_file(
                src,
                flang=flang,
                sema=True,
                extra_args=extra,
                module_dir=moddir,
            )
            annotate_tree(root)
            tu = lower_program(root, source_file=str(src))
            plumb_state(tu)
            results[src] = emit_translation_unit(tu)
    return results


def _needs_cpp(path: Path) -> bool:
    """An uppercase-``F`` suffix (``.F``, ``.F90``, ...) means the file
    expects C preprocessing; the ``-fc1`` frontend does not infer this
    from the extension the way the driver does, so request it explicitly."""
    return "F" in path.suffix[1:]


def _dependency_order(
    paths: list[Path], *, flang: str | None
) -> list[Path]:
    """Order ``paths`` so a file's module providers come before it.

    Cycles (mutually dependent modules) are broken arbitrarily; flang
    still resolves them as long as each ``.mod`` is written before it is
    read, which the source order within a cycle approximates.
    """
    defines: dict[str, Path] = {}
    uses: dict[Path, set[str]] = {}
    for src in paths:
        extra = ["-cpp"] if _needs_cpp(src) else []
        # ``-no-sema`` never needs the .mod files, so it always succeeds.
        root = parse_fortran_file(src, flang=flang, sema=False, extra_args=extra)
        for mod in _defined_modules(root):
            defines.setdefault(mod.lower(), src)
        uses[src] = {m.lower() for m in _used_modules(root)}

    adj: dict[Path, set[Path]] = {p: set() for p in paths}
    for src in paths:
        for mod in uses.get(src, set()):
            provider = defines.get(mod)
            if provider is not None and provider != src:
                adj[src].add(provider)

    order: list[Path] = []
    visited: set[Path] = set()
    on_stack: set[Path] = set()

    def visit(node: Path) -> None:
        if node in visited:
            return
        if node in on_stack:
            return  # cycle — leave for the source-order fallback
        on_stack.add(node)
        for dep in sorted(adj[node], key=lambda p: str(p)):
            visit(dep)
        on_stack.discard(node)
        visited.add(node)
        order.append(node)

    for src in paths:
        visit(src)
    return order


def _defined_modules(root: Node) -> list[str]:
    names: list[str] = []
    for kind in ("Module", "Submodule"):
        for mod in root.find_all(kind):
            stmt = mod.find_first(kind + "Stmt")
            name = stmt.find_first("Name") if stmt is not None else None
            if name is not None and name.fortran:
                names.append(name.fortran)
    return names


def _used_modules(root: Node) -> list[str]:
    names: list[str] = []
    for use in root.find_all("UseStmt"):
        name = use.find_first("Name")
        if name is not None and name.fortran:
            names.append(name.fortran)
    return names
