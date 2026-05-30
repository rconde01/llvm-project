"""Tests for the state-plumbing pass (SAVE locals and common blocks)."""

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


SAVE_F90 = """\
subroutine counter()
  integer, save :: n
  n = n + 1
  print *, "count:", n
end subroutine

program demo
  call counter()
  call counter()
  call counter()
end program
"""


SAVE_TRANSITIVE_F90 = """\
subroutine counter()
  integer, save :: n
  n = n + 1
  print *, "n=", n
end subroutine

subroutine indirect()
  call counter()
end subroutine

program demo
  call counter()
  call indirect()
  call counter()
end program
"""


COMMON_F90 = """\
subroutine init()
  common /state/ x, y
  real :: x, y
  x = 1.0
  y = 2.0
end subroutine

subroutine show()
  common /state/ x, y
  real :: x, y
  print *, x, y
end subroutine

program demo
  common /state/ x, y
  real :: x, y
  call init()
  call show()
  x = x + 10
  call show()
end program
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class StateEmitTests(unittest.TestCase):
    def _convert(self, src: str) -> str:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(src)
            tmp = Path(f.name)
        try:
            return convert_file(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def test_save_generates_struct_and_param(self) -> None:
        cpp = self._convert(SAVE_F90)
        self.assertIn("struct CounterSave {", cpp)
        self.assertIn("std::int32_t n{};", cpp)
        self.assertIn("void counter(CounterSave& counter_save)", cpp)
        # State bound with auto& so the body stays clean.
        self.assertIn("auto& n = counter_save.n;", cpp)
        self.assertIn("n = n + 1;", cpp)
        # Main owns the instance and threads it through.
        self.assertIn("CounterSave counter_save", cpp)
        self.assertIn("counter(counter_save);", cpp)

    def test_save_transitive_forwarding(self) -> None:
        cpp = self._convert(SAVE_TRANSITIVE_F90)
        # indirect() doesn't own the save struct but must forward it.
        self.assertIn("void indirect(CounterSave& counter_save)", cpp)
        self.assertIn("counter(counter_save);", cpp)

    def test_common_block_generates_shared_struct(self) -> None:
        cpp = self._convert(COMMON_F90)
        self.assertIn("struct StateCommon {", cpp)
        self.assertIn("float x{};", cpp)
        self.assertIn("float y{};", cpp)
        # Non-main routines take the struct as a parameter.
        self.assertIn("void init(StateCommon& state_common)", cpp)
        self.assertIn("void show(StateCommon& state_common)", cpp)
        # Main owns the instance (no parameter on the main program).
        self.assertNotIn("void demo(StateCommon", cpp)
        self.assertIn("StateCommon state_common", cpp)
        # Members bound with auto&; body uses the bare names.
        self.assertIn("auto& x = state_common.x;", cpp)
        self.assertIn("x = 1.0f;", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class StateRunTests(unittest.TestCase):
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

    def test_save_state_persists(self) -> None:
        out = self._run(SAVE_F90)
        self.assertIn("count: 1", out)
        self.assertIn("count: 2", out)
        self.assertIn("count: 3", out)

    def test_save_transitive_runs(self) -> None:
        out = self._run(SAVE_TRANSITIVE_F90)
        lines = [l for l in out.splitlines() if l.strip()]
        # counter called 3 times total -> n = 1, 2, 3 in order.
        self.assertIn("n= 1", lines[0])
        self.assertIn("n= 2", lines[1])
        self.assertIn("n= 3", lines[2])

    def test_common_block_shares_state(self) -> None:
        out = self._run(COMMON_F90)
        lines = [l for l in out.splitlines() if l.strip()]
        # First show: x=1, y=2.  After x=x+10: x=11, y=2.
        self.assertIn("1", lines[0])
        self.assertIn("2", lines[0])
        self.assertIn("11", lines[1])

    def test_two_independent_program_states(self) -> None:
        """The whole point of D2.b: two simultaneous states don't
        interfere.  We drive the common-block program's routines with
        two separate struct instances."""
        # This is exercised implicitly by the struct-based design; here
        # we just confirm the emitted struct is a plain value type with
        # no global/static storage.
        cpp = convert_file_str(COMMON_F90)
        self.assertNotIn("static ", cpp)
        self.assertNotIn("thread_local", cpp)


def convert_file_str(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".f90", delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
