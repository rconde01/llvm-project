"""Tests for rank>=2 / mixed array sections and whole-array list output."""

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


SECTIONS_F90 = """\
program p2
  implicit none
  integer :: a(3,3), i, j
  do i = 1, 3
    do j = 1, 3
      a(i,j) = i*10 + j
    end do
  end do
  print *, a(2, :)
  print *, a(:, 1)
  print *, sum(a(1:2, 2:3))
end program
"""


WHOLE_ARRAY_F90 = """\
program wp
  integer :: a(3)
  a = [1, 2, 3]
  print *, a
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
class SectionEmitTests(unittest.TestCase):
    def test_no_todo_marker(self) -> None:
        cpp = _convert(SECTIONS_F90)
        self.assertNotIn("TODO", cpp)

    def test_row_section_uses_slice(self) -> None:
        cpp = _convert(SECTIONS_F90)
        # a(2, :) -> fixed first index, Slice over the (1-based) 2nd dim.
        self.assertIn(
            "a.section(2, fortran::Slice{a.lbound(2), a.ubound(2), 1})", cpp
        )

    def test_block_section_uses_two_slices(self) -> None:
        cpp = _convert(SECTIONS_F90)
        self.assertIn(
            "a.section(fortran::Slice{1, 2, 1}, fortran::Slice{2, 3, 1})", cpp
        )


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class SectionRunTests(unittest.TestCase):
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

    def test_sections_run(self) -> None:
        # a(i,j) = i*10+j.
        lines = self._run(SECTIONS_F90).splitlines()
        self.assertEqual(lines[0].split(), ["21", "22", "23"])  # a(2,:)
        self.assertEqual(lines[1].split(), ["11", "21", "31"])  # a(:,1)
        self.assertEqual(lines[2].split()[0], "70")  # sum(a(1:2,2:3))

    def test_whole_array_print_runs(self) -> None:
        self.assertEqual(self._run(WHOLE_ARRAY_F90).split(), ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
