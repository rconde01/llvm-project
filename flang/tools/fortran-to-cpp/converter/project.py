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
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from flang_ast import FlangError, annotate_tree, parse_fortran_file
from flang_ast.nodes import Node

from .emit import emit_shared_header, emit_translation_unit
from .errors import ConversionError
from .ir import IRTranslationUnit
from .lowering import (
    _drop_external_function_locals,
    _infer_readonly_scalar_params,
    _materialize_value_args,
    _prepend_block_data_calls_to_main,
    _reshape_sequence_associated_args,
    lower_program,
)
from .prepass import sanitized_source
from .state_plumbing import plumb_state

#: Name of the generated header that carries the project's shared structs
#: (modules, derived types, common blocks) and all subprogram prototypes.
SHARED_HEADER_NAME = "fortran_modules.hpp"
_SHARED_HEADER_GUARD = "FORTRAN_TO_CPP_MODULES_HPP"


def convert_files(
    sources: Iterable[str | Path],
    *,
    flang: str | None = None,
    header_name: str = SHARED_HEADER_NAME,
    tolerant: bool = False,
) -> dict[Path, str]:
    """Translate several Fortran files, honoring inter-module ``USE`` deps.

    Returns a mapping from output path to generated text: one ``.cpp`` per
    source plus a shared header (``header_name``) that defines the project's
    module / derived-type / common-block structs and every subprogram
    prototype.  Each ``.cpp`` ``#include``\\s that header, so cross-file
    references to module data and procedures resolve to one definition.

    Files are parsed in dependency order through a shared module directory
    (one file may ``USE`` a module defined in another), then state-plumbed
    as a *single* program so that cross-file call signatures agree.
    """
    paths = [Path(s) for s in sources]
    order = _dependency_order(paths, flang=flang)

    per_file: dict[Path, IRTranslationUnit] = {}
    with tempfile.TemporaryDirectory(prefix="f2cpp-mods-") as moddir:
        for src in order:
            extra = ["-I", moddir]
            if _needs_cpp(src):
                extra.append("-cpp")
            try:
                with sanitized_source(src) as parse_path:
                    root = parse_fortran_file(
                        parse_path,
                        flang=flang,
                        sema=True,
                        extra_args=extra,
                        module_dir=moddir,
                    )
                    annotate_tree(root)
                    per_file[src] = lower_program(root, source_file=str(src))
            except FlangError as exc:
                # flang couldn't parse/analyze this file (bad encoding,
                # a sema error, or a dependency that itself failed).  Skip
                # it and still convert the rest of the project rather than
                # aborting the whole run.
                _warn_skip(src, exc)
                continue
            except ConversionError as exc:
                # The converter doesn't model some construct in this file.
                # Normally that's a hard error (the caller wants to know),
                # but a batch/corpus run (``tolerant``) skips the file and
                # converts the rest -- the caller filters the survivors.
                if not tolerant:
                    raise
                _warn_skip(src, exc)
                continue

    # Plumb persistent/scratch state across the *whole* program so that a
    # routine in one file and its callers in another agree on the state
    # parameters threaded between them.
    combined = _combine(per_file.values())
    # Sequence-association reshaping needs the whole program: a rank-1
    # actual may be passed to a higher-rank dummy declared in another
    # file.  (Idempotent w.r.t. the per-file pass run during lowering.)
    # A routine may call a function defined in *another* file; the
    # per-file drop pass couldn't see it, so its result-type declaration
    # still shadows the function as a scalar local.  Re-run now that every
    # file's subprograms are visible as one program.
    _drop_external_function_locals(combined)
    _reshape_sequence_associated_args(combined)
    # Re-run const inference over the whole program: a scalar dummy a
    # routine never writes locally may still be passed to a callee in
    # another file that writes it, which the per-file run could not see.
    _infer_readonly_scalar_params(combined)
    # Constant/expression actuals passed to a modifiable dummy in another
    # file need the same copy-in temporary; run on the whole program so
    # cross-file call signatures are visible.  (Before state plumbing, so
    # user args still align 1:1 with user params.)
    _materialize_value_args(combined)
    # Dummy procedures need no signature inference: a procedure-taking
    # routine is emitted as a function template and each actual is wrapped in
    # a generic lambda, so the compiler deduces every callback type.
    # Re-run the BLOCK DATA prelude insert now that all files' main
    # programs and BLOCK DATA units are visible together: a per-file
    # lower_program could only see its own file's BLOCK DATAs, but in
    # the IRI configuration the main program lives in iritest.f while
    # GTD7BK lives in cira.f -- the single-file pass missed the call.
    _prepend_block_data_calls_to_main(combined)
    plumb_state(combined)

    results: dict[Path, str] = {
        Path(header_name): emit_shared_header(combined, guard=_SHARED_HEADER_GUARD)
    }
    for src in order:
        if src in per_file:  # skipped (unparseable) files contribute nothing
            results[src] = emit_translation_unit(
                per_file[src], shared_header=header_name
            )
    return results


def _warn_skip(src: Path, exc: FlangError) -> None:
    detail = (exc.stderr or str(exc)).strip().splitlines()
    first = detail[0] if detail else str(exc)
    print(
        f"fortran-to-cpp: skipping {src} (flang could not process it): {first}",
        file=sys.stderr,
    )


def _combine(tus: Iterable[IRTranslationUnit]) -> IRTranslationUnit:
    """A single translation unit referencing every file's subprograms and
    the deduplicated set of shared structs, for whole-program plumbing."""
    combined = IRTranslationUnit()
    seen_mod: set[str] = set()
    seen_dt: set[str] = set()
    for tu in tus:
        combined.subprograms.extend(tu.subprograms)
        for m in tu.modules:
            if m.cpp_type not in seen_mod:
                seen_mod.add(m.cpp_type)
                combined.modules.append(m)
        for dt in tu.derived_types:
            if dt.cpp_type not in seen_dt:
                seen_dt.add(dt.cpp_type)
                combined.derived_types.append(dt)
    return combined


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
        # ``-no-sema`` doesn't need the .mod files, but a file flang can't
        # even scan (e.g. a stray DOS ^Z byte) still fails; treat it as
        # defining/using nothing so the rest of the project proceeds (the
        # later sema parse will skip it too, with a warning).
        try:
            with sanitized_source(src) as parse_path:
                root = parse_fortran_file(
                    parse_path, flang=flang, sema=False, extra_args=extra
                )
        except FlangError:
            uses[src] = set()
            continue
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
