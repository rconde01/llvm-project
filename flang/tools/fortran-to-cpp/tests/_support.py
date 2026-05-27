"""Shared helpers for the per-construct tests.

Each construct test is a small Fortran source exercising *one* feature,
converted to C++ and (when a C++20 compiler is present) compiled and run
to confirm the behavior matches Fortran.  Keeping the harness here lets
each test file read as just "the Fortran in, the assertions out".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from converter import convert_file
from converter.project import convert_files

RUNTIME_INCLUDE = Path(__file__).resolve().parent.parent / "runtime" / "include"


def have_flang() -> bool:
    return bool(
        os.environ.get("FLANG")
        or shutil.which("flang-new")
        or shutil.which("flang")
    )


def have_cxx() -> bool:
    return any(shutil.which(n) for n in ("c++", "g++", "clang++"))


def _cxx() -> str | None:
    return shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")


def convert(src: str, *, suffix: str = ".f90") -> str:
    """Convert a Fortran snippet to C++ text."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def run(src: str, *, suffix: str = ".f90") -> str:
    """Convert, compile and run a Fortran snippet; return its stdout.

    Raises AssertionError (with the generated C++) if compilation or
    execution fails, so a failing construct test points straight at the
    offending translation.
    """
    cxx = _cxx()
    assert cxx is not None, "no C++ compiler"
    with tempfile.TemporaryDirectory() as d:
        src_path = Path(d) / ("in" + suffix)
        src_path.write_text(src)
        cpp = Path(d) / "out.cpp"
        cpp.write_text(convert_file(src_path))
        exe = Path(d) / "out"
        comp = subprocess.run(
            [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE), str(cpp), "-o", str(exe)],
            capture_output=True,
            text=True,
        )
        if comp.returncode != 0:
            raise AssertionError(
                "compile failed:\n" + comp.stderr + "\n--- generated ---\n"
                + cpp.read_text()
            )
        result = subprocess.run([str(exe)], capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError("run failed:\n" + result.stderr)
        return result.stdout


def convert_project(src: str, *, suffix: str = ".f90") -> str:
    """Convert a snippet through the whole-program path and return all of
    the generated C++ (header + source) concatenated.

    Use this for features that only the cross-routine passes implement —
    e.g. dummy-procedure signature inference — which ``convert`` (the
    single-file path) does not run.
    """
    with tempfile.TemporaryDirectory() as d:
        src_path = Path(d) / ("in" + suffix)
        src_path.write_text(src)
        outputs = convert_files([src_path])
        return "\n".join(outputs.values())


def run_project(src: str, *, suffix: str = ".f90") -> str:
    """Convert through the whole-program path, compile and run; return
    stdout.  Writes every generated output (shared header + source) so the
    source's ``#include`` of the header resolves."""
    cxx = _cxx()
    assert cxx is not None, "no C++ compiler"
    with tempfile.TemporaryDirectory() as d:
        src_path = Path(d) / ("in" + suffix)
        src_path.write_text(src)
        outputs = convert_files([src_path])
        main_cpp: Path | None = None
        for name, text in outputs.items():
            if Path(name).suffix in (".hpp", ".h"):
                (Path(d) / Path(name).name).write_text(text)  # shared header
            else:
                main_cpp = Path(d) / "out.cpp"
                main_cpp.write_text(text)
        assert main_cpp is not None, "no .cpp output produced"
        exe = Path(d) / "out"
        comp = subprocess.run(
            [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE), "-I", str(d),
             str(main_cpp), "-o", str(exe)],
            capture_output=True,
            text=True,
        )
        if comp.returncode != 0:
            raise AssertionError(
                "compile failed:\n" + comp.stderr + "\n--- generated ---\n"
                + main_cpp.read_text()
            )
        result = subprocess.run([str(exe)], capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError("run failed:\n" + result.stderr)
        return result.stdout
