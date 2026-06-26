"""Tests for array sections (a(1:5), a(2:10:2)) in assignment & exprs."""

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


SEC_F90 = """\
program sec
  integer :: a(10), b(5)
  integer :: i
  do i = 1, 10
    a(i) = i
  end do
  b = a(2:10:2)
  a(1:5) = 0
  print *, b(1), b(5), a(1), a(6)
end program
"""


SEC_EXPR_F90 = """\
program sec2
  integer :: a(10), b(10)
  integer :: i, s
  do i = 1, 10
    a(i) = i
  end do
  b = 0
  b(1:5) = a(1:5) + a(6:10)
  s = sum(a(1:5))
  print *, b(1), b(5), s
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
    def test_strided_section_assignment_expands(self) -> None:
        cpp = _convert(SEC_F90)
        # b = a(2:10:2) -> position loop mapping k to a(2 + k*2).
        self.assertIn("a(2 + _k", cpp)

    def test_section_target_assignment(self) -> None:
        cpp = _convert(SEC_F90)
        # a(1:5) = 0 -> a(1 + k) = 0.
        self.assertRegex(cpp, r"a\(1 \+ _k\d\) = 0;")

    def test_section_in_elementwise_expr(self) -> None:
        cpp = _convert(SEC_EXPR_F90)
        # b(1:5) = a(1:5) + a(6:10)
        self.assertRegex(
            cpp, r"b\(1 \+ _k\d\) = a\(1 \+ _k\d\) \+ a\(6 \+ _k\d\);"
        )

    def test_section_as_reduction_arg_uses_view(self) -> None:
        cpp = _convert(SEC_EXPR_F90)
        # sum(a(1:5)) -> ftn::sum(a.section(1, 5))
        self.assertIn("ftn::sum(a.section(1, 5))", cpp)


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
        out = self._run(SEC_F90)
        # b=a(2:10:2): b(1)=2,b(5)=10; a(1:5)=0: a(1)=0,a(6)=6.
        self.assertEqual(out.split(), ["2", "10", "0", "6"])

    def test_section_expr_run(self) -> None:
        out = self._run(SEC_EXPR_F90)
        # b(1)=a(1)+a(6)=7; b(5)=a(5)+a(10)=15; sum(a(1:5))=15.
        self.assertEqual(out.split(), ["7", "15", "15"])


if __name__ == "__main__":
    unittest.main()
