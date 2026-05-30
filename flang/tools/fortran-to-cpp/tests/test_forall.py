"""Tests for FORALL statements and constructs (lowered to loop nests)."""

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


FORALL_F90 = """\
program p7
  implicit none
  integer :: a(5), b(3,3), i, j
  forall (i=1:5) a(i) = i*i
  forall (i=1:3, j=1:3) b(i,j) = i*10 + j
  forall (i=1:3)
    b(i,i) = 0
  end forall
  print *, a(3), a(5)
  print *, b(2,3), b(1,1), b(3,3)
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
class ForallEmitTests(unittest.TestCase):
    def test_no_todo_marker(self) -> None:
        cpp = _convert(FORALL_F90)
        self.assertNotIn("TODO", cpp)

    def test_single_index_forall_is_a_loop(self) -> None:
        cpp = _convert(FORALL_F90)
        self.assertIn("for (fortran::index_t i = 1; i <= 5; ++i)", cpp)
        self.assertIn("a(i) = i * i;", cpp)

    def test_multi_index_forall_is_nested_loops(self) -> None:
        cpp = _convert(FORALL_F90)
        self.assertIn("for (fortran::index_t i = 1; i <= 3; ++i)", cpp)
        self.assertIn("for (fortran::index_t j = 1; j <= 3; ++j)", cpp)
        self.assertIn("b(i, j) = i * 10 + j;", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ForallRunTests(unittest.TestCase):
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

    def test_forall_runs(self) -> None:
        lines = self._run(FORALL_F90).splitlines()
        self.assertEqual(lines[0].split(), ["9", "25"])  # a(3), a(5)
        # b(i,j)=i*10+j then diagonal zeroed: b(2,3)=23, b(1,1)=0, b(3,3)=0.
        self.assertEqual(lines[1].split(), ["23", "0", "0"])


if __name__ == "__main__":
    unittest.main()
