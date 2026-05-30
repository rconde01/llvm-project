"""Tests for type-bound procedures (``contains`` in a derived type).

A type-bound procedure ``obj%method(args)`` is lowered to a free
function call ``method(obj, args)`` following Fortran's PASS convention:
the passed-object dummy (conventionally named ``this``) becomes the
first parameter.  ``this`` is a C++ keyword, so it is sanitized to
``this_``.
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


COUNTER_F90 = """\
module counters
  implicit none
  type :: Counter
    integer :: n = 0
  contains
    procedure :: increment
    procedure :: get
  end type
contains
  subroutine increment(this)
    class(Counter), intent(inout) :: this
    this%n = this%n + 1
  end subroutine
  integer function get(this)
    class(Counter), intent(in) :: this
    get = this%n
  end function
end module

program main
  use counters
  type(Counter) :: c
  call c%increment()
  call c%increment()
  print *, c%get()
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
class TypeBoundEmitTests(unittest.TestCase):
    def test_type_becomes_struct_with_initializer(self) -> None:
        cpp = _convert(COUNTER_F90)
        self.assertIn("struct Counter {", cpp)
        self.assertIn("std::int32_t n = 0;", cpp)

    def test_passed_object_is_first_param(self) -> None:
        cpp = _convert(COUNTER_F90)
        # ``this`` is a C++ keyword, sanitized to ``this_``.
        self.assertIn("void increment(Counter& this_)", cpp)
        self.assertIn("std::int32_t get(const Counter& this_)", cpp)

    def test_call_is_rewritten_to_free_function(self) -> None:
        cpp = _convert(COUNTER_F90)
        # c%increment() -> increment(c); c%get() -> get(c).
        self.assertIn("increment(c);", cpp)
        self.assertIn("get(c)", cpp)

    def test_program_named_main_does_not_collide(self) -> None:
        cpp = _convert(COUNTER_F90)
        # A Fortran ``program main`` must not emit ``void main()`` and
        # collide with the C++ ``int main()`` entry point.
        self.assertNotIn("void main(", cpp)
        self.assertIn("void main_program(", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class TypeBoundRunTests(unittest.TestCase):
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

    def test_counter_runs(self) -> None:
        out = self._run(COUNTER_F90)
        # increment twice from 0 -> 2.
        self.assertEqual(out.split()[0], "2")


if __name__ == "__main__":
    unittest.main()
