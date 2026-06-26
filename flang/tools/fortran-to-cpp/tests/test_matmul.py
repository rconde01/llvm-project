"""Tests for MATMUL / TRANSPOSE (array-returning intrinsics)."""

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


RESHAPE_F90 = """\
program rs
  integer :: m(2,3)
  m = reshape([1, 2, 3, 4, 5, 6], [2, 3])
  print *, m(1,1), m(2,1), m(1,2), m(2,3)
end program
"""


MM_F90 = """\
program mm
  real :: a(2,3), b(3,2), c(2,2)
  integer :: i, j
  do j = 1, 3
    do i = 1, 2
      a(i,j) = (i-1)*3 + j
    end do
  end do
  b = transpose(a)
  c = matmul(a, b)
  print *, c(1,1), c(2,2), b(3,1)
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
class MatmulEmitTests(unittest.TestCase):
    def test_array_returning_calls_are_move_assigned(self) -> None:
        cpp = _convert(MM_F90)
        # NOT expanded into element loops; plain move-assignment.
        self.assertIn("b = ftn::transpose(a);", cpp)
        self.assertIn("c = ftn::matmul(a, b);", cpp)

    def test_reshape_flattens_shape_to_dim_args(self) -> None:
        cpp = _convert(RESHAPE_F90)
        # shape [2,3] becomes trailing dim args so the rank is deduced.
        self.assertIn(
            "ftn::reshape(ftn::array_of(1, 2, 3, 4, 5, 6), 2, 3)",
            cpp,
        )


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class MatmulRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(MM_F90)
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
            # a=[[1,2,3],[4,5,6]]; b=transpose(a); c=a*b.
            # c(1,1)=1+4+9=14; c(2,2)=16+25+36=77; b(3,1)=3.
            self.assertEqual(run.stdout.split(), ["14", "77", "3"])

    def test_reshape_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(RESHAPE_F90)
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
            # column-major fill of 2x3: m(1,1)=1,m(2,1)=2,m(1,2)=3,m(2,3)=6.
            self.assertEqual(run.stdout.split(), ["1", "2", "3", "6"])


if __name__ == "__main__":
    unittest.main()
