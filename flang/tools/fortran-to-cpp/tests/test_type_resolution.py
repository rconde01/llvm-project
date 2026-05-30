"""Tests for dumper-based type resolution.

flang resolves every variable's type (kind, custom IMPLICIT, star-kinds,
host/use association) in its symbol table; the JSON dumper now emits that
on each Name as ``type`` / ``rank``, and the converter uses it instead of
re-deriving types.  This fixes cases the parse-tree spelling alone got
wrong (``integer*8``) or couldn't know (``implicit double precision``).
"""

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


DP_F = """\
      program ty
      implicit double precision (a-h,o-z)
      dimension d(5)
      integer*8 big
      x = 1.5
      n = 3
      d(1) = x
      big = 10
      print *, x, n, d(1), big
      end
"""


def _convert(src: str, suffix: str = ".f") -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def _convert_f90(src: str) -> str:
    return _convert(src, ".f90")


@unittest.skipUnless(_have_flang(), "flang binary not available")
class TypeResolutionEmitTests(unittest.TestCase):
    def test_implicit_double_precision(self) -> None:
        cpp = _convert(DP_F)
        # implicit double precision (a-h,o-z): x and d are double.
        self.assertIn("double x{};", cpp)
        self.assertIn("fortran::Array<double, 1> d{{5}};", cpp)

    def test_in_letters_still_integer(self) -> None:
        cpp = _convert(DP_F)
        # n falls under the default i-n integer rule (not overridden).
        self.assertIn("std::int32_t n{};", cpp)

    def test_star_kind_explicit_decl(self) -> None:
        cpp = _convert(DP_F)
        # integer*8 -> 64-bit, resolved from the symbol table.
        self.assertIn("std::int64_t big{};", cpp)


CLASSIFY_F90 = """\
module m
  integer :: gcount
contains
  subroutine bump()
    gcount = gcount + 1
  end subroutine
end module
program p
  use m
  real :: a(5)
  integer :: i
  i = 2
  a(i) = sqrt(real(i))
  call bump()
  print *, a(i), gcount
end program
"""


SHAPE_F = """\
      program shp
      parameter (nmax=4)
      dimension d(5), e(0:9), g(nmax)
      common /b/ narr(3)
      d(1) = 1.0
      e(0) = 2.0
      g(1) = 3.0
      narr(1) = 7
      print *, d(1), e(0), g(1), narr(1)
      end
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class ShapeFromSymbolTests(unittest.TestCase):
    """Array shapes are sized from the resolved symbol (constant-folded,
    incl. PARAMETER bounds and arbitrary lower bounds) — no DIMENSION read."""

    def test_constant_and_parameter_bounds(self) -> None:
        cpp = _convert(SHAPE_F)
        self.assertIn("fortran::Array<float, 1> d{{5}};", cpp)
        # g(nmax) with nmax==4 is folded.
        self.assertIn("fortran::Array<float, 1> g{{4}};", cpp)

    def test_arbitrary_lower_bound(self) -> None:
        cpp = _convert(SHAPE_F)
        # e(0:9): lower 0, extent 10.
        self.assertIn("fortran::Array<float, 1> e{{0}, {10}};", cpp)


@unittest.skipUnless(_have_flang(), "flang binary not available")
class ClassificationTests(unittest.TestCase):
    """Symbol facts replace the variable-vs-procedure / module-var
    heuristics: procedures and module/host state are not mis-declared as
    local variables."""

    def test_procedures_not_declared_as_locals(self) -> None:
        cpp = _convert_f90(CLASSIFY_F90)
        # sqrt / real / bump are procedures, not variables.
        self.assertNotIn("sqrt{", cpp)
        self.assertNotIn("float real", cpp)

    def test_module_var_not_a_program_local(self) -> None:
        cpp = _convert_f90(CLASSIFY_F90)
        # gcount is module state (threaded), never a local of the program.
        prog = cpp[cpp.index("void p("):] if "void p(" in cpp else cpp
        self.assertNotIn("std::int32_t gcount{};", prog)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class TypeResolutionRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(DP_F))
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
            # x=1.5, n=3, d(1)=1.5, big=10.
            self.assertEqual(run.stdout.split(), ["1.5", "3", "1.5", "10"])


if __name__ == "__main__":
    unittest.main()
