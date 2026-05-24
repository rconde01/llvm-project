"""Tests for array constructors [e1, e2, ...] / (/ ... /)."""

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


AC_F90 = """\
program ac
  integer :: a(3)
  real :: r(2)
  a = [10, 20, 30]
  r = (/ 1.5, 2.5 /)
  print *, a(1), a(3), sum([1, 2, 3, 4]), r(2)
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
class ArrayConstructorEmitTests(unittest.TestCase):
    def test_assignment_uses_array_of_move(self) -> None:
        cpp = _convert(AC_F90)
        self.assertIn("a = fortran::array_of({10, 20, 30});", cpp)

    def test_paren_slash_form(self) -> None:
        cpp = _convert(AC_F90)
        self.assertIn("fortran::array_of({1.5f, 2.5f})", cpp)

    def test_constructor_as_intrinsic_arg(self) -> None:
        cpp = _convert(AC_F90)
        self.assertIn("fortran::sum(fortran::array_of({1, 2, 3, 4}))", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ArrayConstructorRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(AC_F90)
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
            parts = run.stdout.split()
            self.assertEqual(parts[0], "10")
            self.assertEqual(parts[1], "30")
            self.assertEqual(parts[2], "10")  # sum 1..4
            self.assertEqual(parts[3], "2.5")


if __name__ == "__main__":
    unittest.main()
