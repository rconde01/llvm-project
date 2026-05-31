"""Tests for DATA statement initialization."""

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


DATA_F90 = """\
program dt
  integer :: a(3), n
  real :: x
  data a /10, 20, 30/
  data n, x /5, 3.14/
  print *, a(1), a(3), n, x
end program
"""


REPEAT_F90 = """\
program dt
  integer :: k(6)
  data k /1, 2, 3*7, 9/
  print *, k(1), k(3), k(4), k(6)
end program
"""


# A nested implied-do over a 2-D slice — every subscript is a loop
# variable.  Iterates innermost-first (column-major), matching the order
# values are listed: a(1,1), a(2,1), a(3,1), a(1,2), ...
IDO_2D_F90 = """\
program dt
  integer :: a(3, 2)
  data ((a(i, j), i = 1, 3), j = 1, 2) /10, 20, 30, 40, 50, 60/
  print *, a(1, 1), a(3, 1), a(1, 2), a(3, 2)
end program
"""


# A single implied-do whose array element mixes *constant* subscripts
# with the loop variable: ``(c(1, 1, j), j = 1, 4)`` fills the slice
# c(1,1,1..4).  This is the form the IRI Te models (TEBA / ELTEIK) use
# for their 3-D coefficient tables; the constant subscripts must stay
# fixed while only ``j`` advances.
IDO_CONST_SUB_F90 = """\
program dt
  real :: c(2, 2, 4)
  data (c(1, 1, j), j = 1, 4) /1.5, 2.5, 3.5, 4.5/
  data (c(2, 1, j), j = 1, 4) /5.5, 6.5, 7.5, 8.5/
  print *, c(1, 1, 1), c(1, 1, 4), c(2, 1, 1), c(2, 1, 4)
end program
"""


# Single array elements and an element list as DATA objects (not a whole
# array).  Each names exactly one slot and consumes one value.
ELEM_F90 = """\
program dt
  real :: a(5)
  data a(2) /7.0/
  data a(1), a(4) /5.0, 9.0/
  print *, a(1), a(2), a(4)
end program
"""


# Implied-do whose bound is a PARAMETER (flang folds it, but the bound
# node is a name reference, not a literal) and one with an explicit
# stride ``i = 1, 5, 2`` (fills a(1), a(3), a(5)).
IDO_PARAM_BOUND_F90 = """\
program dt
  integer, parameter :: n = 4
  real :: a(4)
  data (a(i), i = 1, n) /10.0, 20.0, 30.0, 40.0/
  print *, a(1), a(4)
end program
"""

IDO_STRIDE_F90 = """\
program dt
  real :: a(5)
  data (a(i), i = 1, 5, 2) /1.0, 3.0, 5.0/
  print *, a(1), a(3), a(5)
end program
"""


def _convert(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".f90", delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


@unittest.skipUnless(_have_flang(), "flang binary not available")
class DataEmitTests(unittest.TestCase):
    def test_array_data_uses_array_of(self) -> None:
        cpp = _convert(DATA_F90)
        self.assertIn("a = fortran::array_of(10, 20, 30);", cpp)

    def test_scalar_data_pairs(self) -> None:
        cpp = _convert(DATA_F90)
        self.assertIn("n = 5;", cpp)
        # The folded literal preserves binary precision; assert just the
        # leading digits and the ``f`` suffix.
        self.assertRegex(cpp, r"x = 3\.14\d*f;")

    def test_data_runs_before_body(self) -> None:
        cpp = _convert(DATA_F90)
        # Inits precede the print statement.
        self.assertLess(cpp.index("a = fortran::array_of"), cpp.index("std::cout"))

    def test_repeat_count_expanded(self) -> None:
        cpp = _convert(REPEAT_F90)
        # ``3*7`` expands to three 7s.
        self.assertIn("k = fortran::array_of(1, 2, 7, 7, 7, 9);", cpp)

    def test_implied_do_2d_column_major(self) -> None:
        cpp = _convert(IDO_2D_F90)
        # Innermost (i) advances fastest: a(1,1), a(2,1), a(3,1), a(1,2)...
        self.assertIn("a(1, 1) = 10;", cpp)
        self.assertIn("a(2, 1) = 20;", cpp)
        self.assertIn("a(3, 1) = 30;", cpp)
        self.assertIn("a(1, 2) = 40;", cpp)
        self.assertIn("a(3, 2) = 60;", cpp)

    def test_implied_do_constant_subscripts(self) -> None:
        cpp = _convert(IDO_CONST_SUB_F90)
        # Constant subscripts stay fixed; only j advances over the slice.
        self.assertIn("c(1, 1, 1) = 1.5f;", cpp)
        self.assertIn("c(1, 1, 4) = 4.5f;", cpp)
        self.assertIn("c(2, 1, 1) = 5.5f;", cpp)
        self.assertIn("c(2, 1, 4) = 8.5f;", cpp)

    def test_single_array_element_objects(self) -> None:
        # ``data a(2) /7/`` and an element list must each emit one
        # assignment (previously silently dropped, leaving zeros).
        cpp = _convert(ELEM_F90)
        self.assertIn("a(2) = 7.f;", cpp)
        self.assertIn("a(1) = 5.f;", cpp)
        self.assertIn("a(4) = 9.f;", cpp)

    def test_implied_do_parameter_bound(self) -> None:
        # The folded PARAMETER bound (n=4) must expand all four elements
        # (flang renders 10.0 as ``1.e1`` etc.) with no dropped initializer.
        cpp = _convert(IDO_PARAM_BOUND_F90)
        self.assertIn("a(1) = 1.e1f;", cpp)
        self.assertIn("a(4) = 4.e1f;", cpp)
        self.assertNotIn("TODO", cpp)

    def test_implied_do_stride(self) -> None:
        # ``i = 1, 5, 2`` fills a(1), a(3), a(5) -- NOT a(1), a(2), a(3).
        cpp = _convert(IDO_STRIDE_F90)
        self.assertIn("a(1) = 1.f;", cpp)
        self.assertIn("a(3) = 3.f;", cpp)
        self.assertIn("a(5) = 5.f;", cpp)
        self.assertNotIn("a(2) =", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class DataRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(DATA_F90)
            cpp = Path(d) / "out.cpp"
            cpp.write_text(convert_file(f))
            exe = Path(d) / "out"
            cxx = (
                shutil.which("c++")
                or shutil.which("g++")
                or shutil.which("clang++")
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
            self.assertEqual(run.stdout.split(), ["10", "30", "5", "3.14"])

    def _build_and_run(self, src: str) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(src)
            cpp = Path(d) / "out.cpp"
            cpp.write_text(convert_file(f))
            exe = Path(d) / "out"
            cxx = (
                shutil.which("c++")
                or shutil.which("g++")
                or shutil.which("clang++")
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
            return run.stdout.split()

    def test_implied_do_2d_runs(self) -> None:
        self.assertEqual(self._build_and_run(IDO_2D_F90), ["10", "30", "40", "60"])

    def test_implied_do_constant_subscripts_runs(self) -> None:
        self.assertEqual(
            self._build_and_run(IDO_CONST_SUB_F90),
            ["1.5", "4.5", "5.5", "8.5"],
        )

    def test_single_array_element_runs(self) -> None:
        self.assertEqual(self._build_and_run(ELEM_F90), ["5", "7", "9"])

    def test_implied_do_parameter_bound_runs(self) -> None:
        self.assertEqual(self._build_and_run(IDO_PARAM_BOUND_F90), ["10", "40"])

    def test_implied_do_stride_runs(self) -> None:
        self.assertEqual(self._build_and_run(IDO_STRIDE_F90), ["1", "3", "5"])


if __name__ == "__main__":
    unittest.main()
