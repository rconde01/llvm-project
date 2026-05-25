"""Tests for FORTRAN 77 implicit typing, DIMENSION, and CONTINUE.

Legacy fixed-form code rarely declares every variable: undeclared names
are typed by the implicit rule (INTEGER if the name starts I-N, else
REAL), arrays get their shape from DIMENSION / COMMON, and CONTINUE is a
no-op.  Modern code with ``implicit none`` is unaffected.
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


F77_F = """\
      program f77
      dimension d(5)
      common /blk/ x, narr(3)
      n = 3
      x = 1.5
      do 10 i = 1, 5
         d(i) = i * 2
   10 continue
      narr(1) = 7
      print *, d(2), n, x, narr(1)
      end
"""


IMPLICIT_NONE_F90 = """\
program p
  implicit none
  integer :: k
  k = 5
  print *, k
end program
"""


def _convert_path(src: str, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


@unittest.skipUnless(_have_flang(), "flang binary not available")
class ImplicitTypingEmitTests(unittest.TestCase):
    def test_implicit_scalars_declared(self) -> None:
        cpp = _convert_path(F77_F, ".f")
        self.assertIn("std::int32_t i{};", cpp)  # I-N -> integer
        self.assertIn("std::int32_t n{};", cpp)

    def test_dimension_array_gets_implicit_element_type(self) -> None:
        cpp = _convert_path(F77_F, ".f")
        # d starts with 'd' -> real; DIMENSION d(5) -> Array<float,1>.
        self.assertIn("fortran::Array<float, 1> d{{5}};", cpp)

    def test_common_members_typed(self) -> None:
        cpp = _convert_path(F77_F, ".f")
        # No unresolved-type placeholder; members get implicit types.
        self.assertNotIn("TODO: type", cpp)
        self.assertIn("float x{};", cpp)
        self.assertIn("fortran::Array<std::int32_t, 1> narr{{3}};", cpp)

    def test_continue_is_noop(self) -> None:
        cpp = _convert_path(F77_F, ".f")
        self.assertNotIn("ContinueStmt", cpp)

    def test_implicit_none_unaffected(self) -> None:
        # A unit with implicit none must not gain synthesized locals.
        cpp = _convert_path(IMPLICIT_NONE_F90, ".f90")
        self.assertEqual(cpp.count("std::int32_t k"), 1)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ImplicitTypingRunTests(unittest.TestCase):
    def test_f77_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert_path(F77_F, ".f"))
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
            # d(2)=4, n=3, x=1.5, narr(1)=7.
            self.assertEqual(run.stdout.split(), ["4", "3", "1.5", "7"])


if __name__ == "__main__":
    unittest.main()
