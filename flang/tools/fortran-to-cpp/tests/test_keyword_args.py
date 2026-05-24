"""Tests for keyword actual arguments and internal (CONTAINS) procedures.

A call written with keyword arguments (``f(b=2, a=1)``) must be
reordered to the positional order of the callee's dummy arguments
(``f(1, 2)``).  Internal procedures (those in a program's or
subprogram's ``CONTAINS`` section) become free functions emitted before
their host.
"""

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


SUBR_KW_F90 = """\
program kw
  implicit none
  real :: r
  call scaled(out=r, x=3.0, factor=2.0)
  print *, r
contains
  subroutine scaled(x, factor, out)
    real, intent(in) :: x, factor
    real, intent(out) :: out
    out = x * factor
  end subroutine
end program
"""


FUNC_KW_F90 = """\
program kw2
  implicit none
  print *, area(3.0, h=4.0)
  print *, area(w=5.0, h=2.0)
contains
  real function area(w, h)
    real, intent(in) :: w, h
    area = w * h
  end function
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
class KeywordArgEmitTests(unittest.TestCase):
    def test_internal_subroutine_is_emitted(self) -> None:
        cpp = _convert(SUBR_KW_F90)
        # Internal procedure becomes a free function, emitted before the
        # host program body.
        self.assertIn("void scaled(", cpp)
        self.assertLess(cpp.index("void scaled("), cpp.index("void kw("))

    def test_subroutine_keyword_args_reordered(self) -> None:
        cpp = _convert(SUBR_KW_F90)
        # Dummy order is (x, factor, out); the all-keyword call must be
        # reordered to that, regardless of source order.
        self.assertIn("scaled(3.0f, 2.0f, r);", cpp)

    def test_function_mixed_and_keyword_args_reordered(self) -> None:
        cpp = _convert(FUNC_KW_F90)
        # area(3.0, h=4.0): positional then keyword -> (3, 4).
        self.assertIn("area(3.0f, 4.0f)", cpp)
        # area(w=5.0, h=2.0): all keyword -> (5, 2).
        self.assertIn("area(5.0f, 2.0f)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class KeywordArgRunTests(unittest.TestCase):
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

    def test_subroutine_keyword_runs(self) -> None:
        # 3 * 2 = 6.
        self.assertEqual(self._run(SUBR_KW_F90).split()[0], "6")

    def test_function_keyword_runs(self) -> None:
        parts = self._run(FUNC_KW_F90).split()
        # 3*4 = 12; 5*2 = 10.
        self.assertEqual(parts[0], "12")
        self.assertEqual(parts[1], "10")


if __name__ == "__main__":
    unittest.main()
