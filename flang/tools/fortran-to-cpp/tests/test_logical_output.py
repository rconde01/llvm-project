"""Tests for list-directed logical output (Fortran prints T / F, not 1 / 0)."""

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


LOGICAL_F90 = """\
program lg
  implicit none
  logical :: a, b
  integer :: n
  a = .true.
  b = .false.
  n = 7
  print *, a, b, (1 < 2), n
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
class LogicalOutputEmitTests(unittest.TestCase):
    def test_logical_items_wrapped(self) -> None:
        cpp = _convert(LOGICAL_F90)
        # Logical variables and comparisons get the T/F formatter.
        self.assertIn("fortran::logical_text(a)", cpp)
        self.assertIn("fortran::logical_text(b)", cpp)
        self.assertIn("fortran::logical_text((1 < 2))", cpp)

    def test_non_logical_items_not_wrapped(self) -> None:
        cpp = _convert(LOGICAL_F90)
        # An integer item is streamed plainly.
        self.assertNotIn("fortran::logical_text(n)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class LogicalOutputRunTests(unittest.TestCase):
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

    def test_logical_output_runs(self) -> None:
        parts = self._run(LOGICAL_F90).split()
        self.assertEqual(parts[0], "T")
        self.assertEqual(parts[1], "F")
        self.assertEqual(parts[2], "T")
        self.assertEqual(parts[3], "7")


if __name__ == "__main__":
    unittest.main()
