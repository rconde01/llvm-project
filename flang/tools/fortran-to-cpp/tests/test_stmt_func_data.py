"""Tests for FORTRAN 77 statement functions and DATA in the execution part."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from converter import convert_file


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


def _have_cxx() -> bool:
    return any(shutil.which(n) for n in ("c++", "g++", "clang++"))


RUNTIME_INCLUDE = Path(__file__).resolve().parent.parent / "runtime" / "include"


STMT_FUNC_F = """\
      program sf
      f(x, y) = x*x + y
      a = 3.0
      b = 2.0
      r = f(a, b)
      print *, r
      end
"""

DATA_EXEC_F = """\
      program dx
      dimension a(3)
      x = 1.0
      data a /10.0, 20.0, 30.0/
      data k /7/
      print *, a(2), k, x
      end
"""


def _convert(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".f", delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def _run(src: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        cpp = Path(d) / "out.cpp"
        cpp.write_text(_convert(src))
        exe = Path(d) / "out"
        cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        assert cxx is not None
        comp = subprocess.run(
            [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE), str(cpp), "-o", str(exe)],
            capture_output=True, text=True, check=False,
        )
        if comp.returncode != 0:
            raise AssertionError(f"compile failed:\n{comp.stderr}\n{cpp.read_text()}")
        run = subprocess.run([str(exe)], capture_output=True, text=True, check=False)
        assert run.returncode == 0, run.stderr
        return run.stdout


@unittest.skipUnless(_have_flang(), "flang binary not available")
class StmtFuncEmitTests(unittest.TestCase):
    def test_lambda(self) -> None:
        cpp = _convert(STMT_FUNC_F)
        self.assertIn(
            "auto f = [&](auto x, auto y) { return x * x + y; };", cpp
        )
        self.assertIn("r = f(a, b);", cpp)

    def test_params_not_declared_as_locals(self) -> None:
        cpp = _convert(STMT_FUNC_F)
        # x / y are the lambda's params, not unit locals.
        self.assertNotIn("float x{};", cpp)
        self.assertNotIn("float y{};", cpp)


@unittest.skipUnless(_have_flang(), "flang binary not available")
class DataExecEmitTests(unittest.TestCase):
    def test_data_in_execution_part_collected(self) -> None:
        cpp = _convert(DATA_EXEC_F)
        self.assertNotIn("does not yet translate", cpp)
        self.assertIn("a = fortran::array_of({10.0f, 20.0f, 30.0f});", cpp)
        self.assertIn("k = 7;", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class RunTests(unittest.TestCase):
    def test_stmt_func_runs(self) -> None:
        # f(3,2) = 3*3 + 2 = 11.
        self.assertEqual(_run(STMT_FUNC_F).split()[0], "11")

    def test_data_exec_runs(self) -> None:
        # a(2)=20, k=7, x=1.
        self.assertEqual(_run(DATA_EXEC_F).split(), ["20", "7", "1"])


if __name__ == "__main__":
    unittest.main()
