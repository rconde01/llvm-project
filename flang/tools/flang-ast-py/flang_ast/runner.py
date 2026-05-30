"""Run flang to obtain a JSON parse tree directly from Fortran source."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from .nodes import Node
from .parser import parse_json_string


class FlangError(RuntimeError):
    """Raised when the flang invocation fails."""

    def __init__(self, message: str, *, stderr: str = "", returncode: int = 0) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


def _resolve_flang(explicit: str | os.PathLike[str] | None) -> str:
    """Locate the flang binary to invoke."""
    if explicit:
        return os.fspath(explicit)
    env = os.environ.get("FLANG")
    if env:
        return env
    for name in ("flang-new", "flang"):
        found = shutil.which(name)
        if found:
            return found
    raise FlangError(
        "Could not find a 'flang' or 'flang-new' executable; set FLANG=/path/to/flang "
        "or pass flang=... explicitly."
    )


def parse_fortran_file(
    path: str | os.PathLike[str],
    *,
    flang: str | os.PathLike[str] | None = None,
    sema: bool = True,
    extra_args: Sequence[str] = (),
    module_dir: str | os.PathLike[str] | None = None,
) -> Node:
    """Run ``flang -fc1 -fdebug-dump-analyzed-tree-json`` on a file.

    Args:
        path: Source file path.
        flang: Override the flang executable.  Defaults to ``$FLANG`` or
            the first of ``flang-new``/``flang`` on ``$PATH``.
        sema: If ``True`` (default) run the semantic checks first so that
            the ``fortran`` fields are populated.  If ``False`` use the
            ``-no-sema`` variant.
        extra_args: Additional ``flang -fc1`` arguments (e.g. ``["-I", "mods"]``).
        module_dir: Where flang writes (and the build reads) ``.mod``
            files.  When ``None`` a throw-away temp dir is used so we
            never pollute the caller's cwd; pass a persistent directory
            to share generated modules across calls (multi-file projects
            where one file ``USE``s a module defined in another).

    Returns:
        The parsed AST root.
    """
    binary = _resolve_flang(flang)
    flag = "-fdebug-dump-analyzed-tree-json" if sema else "-fdebug-dump-analyzed-tree-json-no-sema"
    if module_dir is not None:
        return _run(binary, flag, os.fspath(module_dir), extra_args, path)
    # flang writes generated .mod files to the current working directory
    # by default; redirect them to a throw-away temp dir so we never
    # pollute the caller's cwd.
    with tempfile.TemporaryDirectory(prefix="flang-ast-mod-") as moddir:
        return _run(binary, flag, moddir, extra_args, path)


def _run(
    binary: str,
    flag: str,
    moddir: str,
    extra_args: Sequence[str],
    path: str | os.PathLike[str],
) -> Node:
    cmd = [
        binary,
        "-fc1",
        flag,
        "-module-dir",
        moddir,
        *extra_args,
        os.fspath(path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, cwd=moddir
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise FlangError(
            f"flang failed (exit {proc.returncode}) running {' '.join(cmd)}",
            stderr=proc.stderr,
            returncode=proc.returncode,
        )
    if not proc.stdout.strip():
        raise FlangError(
            "flang produced no AST output; stderr was:\n" + proc.stderr,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )
    return parse_json_string(proc.stdout)


def parse_fortran_source(
    source: str,
    *,
    suffix: str = ".f90",
    flang: str | os.PathLike[str] | None = None,
    sema: bool = True,
    extra_args: Sequence[str] = (),
) -> Node:
    """Parse a Fortran source string by writing it to a temp file first.

    Useful for unit tests and quick experiments.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8"
    ) as f:
        f.write(source)
        tmp = Path(f.name)
    try:
        return parse_fortran_file(
            tmp, flang=flang, sema=sema, extra_args=extra_args
        )
    finally:
        tmp.unlink(missing_ok=True)
