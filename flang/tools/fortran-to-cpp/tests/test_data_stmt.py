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
        self.assertIn("x = 3.14f;", cpp)

    def test_data_runs_before_body(self) -> None:
        cpp = _convert(DATA_F90)
        # Inits precede the print statement.
        self.assertLess(cpp.index("a = fortran::array_of"), cpp.index("std::cout"))

    def test_repeat_count_expanded(self) -> None:
        cpp = _convert(REPEAT_F90)
        # ``3*7`` expands to three 7s.
        self.assertIn("k = fortran::array_of(1, 2, 7, 7, 7, 9);", cpp)


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


if __name__ == "__main__":
    unittest.main()
