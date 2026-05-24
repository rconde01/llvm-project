"""Tests that Fortran array intrinsics lower to fortran:: helpers."""

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


INTRINSICS_F90 = """\
program intr
  integer :: a(5)
  integer :: i, s, mx, mn, np
  do i = 1, 5
    a(i) = i * i
  end do
  s = sum(a)
  mx = maxval(a)
  mn = minval(a)
  np = size(a)
  print *, s, mx, mn, np
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
class IntrinsicEmitTests(unittest.TestCase):
    def test_array_intrinsics_map_to_fortran_ns(self) -> None:
        cpp = _convert(INTRINSICS_F90)
        self.assertIn("fortran::sum(a)", cpp)
        self.assertIn("fortran::maxval(a)", cpp)
        self.assertIn("fortran::minval(a)", cpp)
        self.assertIn("fortran::size(a)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class IntrinsicRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(INTRINSICS_F90)
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
            # a = [1,4,9,16,25]: sum 55, max 25, min 1, size 5.
            parts = run.stdout.split()
            self.assertEqual(parts, ["55", "25", "1", "5"])


if __name__ == "__main__":
    unittest.main()
