"""Tests for the ``parameter (...)`` statement form (named constants)."""

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


PAR_F = """\
      program par
      parameter (n=5, pi=3.14159)
      dimension a(n)
      do i = 1, n
        a(i) = i * pi
      end do
      print *, a(n), n
      end
"""

# Type declared separately from the PARAMETER value.
TYPED_PAR_F = """\
      program tp
      integer m
      parameter (m=3)
      print *, m
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


@unittest.skipUnless(_have_flang(), "flang binary not available")
class ParameterEmitTests(unittest.TestCase):
    def test_constexpr_with_value(self) -> None:
        cpp = _convert(PAR_F)
        self.assertIn("constexpr std::int32_t n = 5;", cpp)
        self.assertIn("constexpr float pi = 3.14159f;", cpp)

    def test_bound_folds_using_parameter(self) -> None:
        cpp = _convert(PAR_F)
        self.assertIn("fortran::Array<float, 1> a{{5}};", cpp)

    def test_type_decl_plus_parameter_not_duplicated(self) -> None:
        cpp = _convert(TYPED_PAR_F)
        # ``integer m`` + ``parameter (m=3)`` -> one constexpr, not a
        # mutable ``m`` plus a constexpr ``m``.
        self.assertIn("constexpr std::int32_t m = 3;", cpp)
        self.assertNotIn("std::int32_t m{};", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ParameterRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(PAR_F))
            exe = Path(d) / "out"
            cxx = (
                shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
            )
            assert cxx is not None
            comp = subprocess.run(
                [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE),
                 str(cpp), "-o", str(exe)],
                capture_output=True, text=True, check=False,
            )
            if comp.returncode != 0:
                self.fail(f"compile failed:\n{comp.stderr}\n{cpp.read_text()}")
            run = subprocess.run(
                [str(exe)], capture_output=True, text=True, check=False
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            parts = run.stdout.split()
            # a(5) = 5 * 3.14159 = 15.708; n = 5.
            self.assertEqual(parts[0], "15.708")
            self.assertEqual(parts[1], "5")


if __name__ == "__main__":
    unittest.main()
