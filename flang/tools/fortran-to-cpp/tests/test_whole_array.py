"""Tests for whole-array assignment expanded into explicit loops."""

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


WHOLE_F90 = """\
program whole
  real :: a(5), b(5), c(5)
  integer :: i
  do i = 1, 5
    b(i) = i
    c(i) = 10.0
  end do
  a = b + c
  a = 2.0 * a
  c = 0.0
  print *, a(1), a(5), c(1)
end program
"""


WHOLE_2D_F90 = """\
program w2
  real :: m(2,3), n(2,3)
  real :: v(4), w(4)
  integer :: i, j
  do j = 1, 3
    do i = 1, 2
      m(i,j) = i + j
    end do
  end do
  n = m * 2.0
  do i = 1, 4
    v(i) = i * 1.0
  end do
  w = sqrt(v)
  v = sum(w)
  print *, n(2,3), w(4), v(1)
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
class WholeArrayEmitTests(unittest.TestCase):
    def test_elementwise_add_expands_to_loop(self) -> None:
        cpp = _convert(WHOLE_F90)
        self.assertIn("for (fortran::index_t _i", cpp)
        # Both operands indexed by the synthesized loop variable.
        self.assertRegex(
            cpp, r"a\(_i\d\) = b\(_i\d\) \+ c\(_i\d\);"
        )

    def test_scalar_broadcast_not_indexed(self) -> None:
        cpp = _convert(WHOLE_F90)
        # ``c = 0.0`` -> ``c(_i) = 0.0f`` (RHS scalar, no indexing).
        self.assertRegex(cpp, r"c\(_i\d\) = 0\.0f;")

    def test_2d_assignment_is_nested_loop(self) -> None:
        cpp = _convert(WHOLE_2D_F90)
        self.assertRegex(
            cpp, r"n\(_i\d, _i\d\) = m\(_i\d, _i\d\) \* 2\.0f;"
        )

    def test_elemental_intrinsic_indexes_arg(self) -> None:
        cpp = _convert(WHOLE_2D_F90)
        # ``w = sqrt(v)`` -> ``w(_i) = fortran::sqrt(v(_i))``
        self.assertRegex(cpp, r"w\(_i\d\) = fortran::sqrt\(v\(_i\d\)\);")

    def test_reduction_arg_not_indexed(self) -> None:
        cpp = _convert(WHOLE_2D_F90)
        # ``v = sum(w)`` -> ``v(_i) = fortran::sum(w)`` (w whole-array).
        self.assertRegex(cpp, r"v\(_i\d\) = fortran::sum\(w\);")


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class WholeArrayRunTests(unittest.TestCase):
    def _run(self, src: str) -> str:
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
            return run.stdout

    def test_whole_array_runs(self) -> None:
        out = self._run(WHOLE_F90)
        parts = out.split()
        # a = 2*(b+c): a(1)=2*(1+10)=22, a(5)=2*(5+10)=30, c=0.
        self.assertEqual(parts[0], "22")
        self.assertEqual(parts[1], "30")
        self.assertEqual(parts[2], "0")

    def test_2d_and_elemental_run(self) -> None:
        out = self._run(WHOLE_2D_F90)
        parts = out.split()
        # n(2,3) = (2+3)*2 = 10; w(4) = sqrt(4) = 2.
        self.assertEqual(parts[0], "10")
        self.assertEqual(parts[1], "2")


if __name__ == "__main__":
    unittest.main()
