"""Tests for fixed-size local arrays hoisted into per-routine workspaces.

A naive translation either puts large local arrays on the stack
(overflow risk) or heap-allocates them on every call (slow).  The
workspace pass hoists fixed-size local arrays into a caller-owned
struct allocated once and threaded through by reference — except in
recursive routines, where each activation needs its own copy.
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


WORKSPACE_F90 = """\
subroutine process(scale)
  real, intent(in) :: scale
  real :: buffer(1000)
  integer :: i
  do i = 1, 1000
    buffer(i) = i * scale
  end do
  print *, buffer(500)
end subroutine

program demo
  call process(2.0)
  call process(3.0)
end program
"""


RECURSIVE_F90 = """\
recursive subroutine descend(depth)
  integer, intent(in) :: depth
  real :: scratch(100)
  scratch(1) = depth
  if (depth > 0) call descend(depth - 1)
  print *, depth, scratch(1)
end subroutine

program demo
  call descend(3)
end program
"""


AUTOMATIC_F90 = """\
subroutine work(n)
  integer, intent(in) :: n
  real :: tmp(n)
  integer :: i
  do i = 1, n
    tmp(i) = i
  end do
  print *, tmp(n)
end subroutine

program demo
  call work(5)
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
class WorkspaceEmitTests(unittest.TestCase):
    def test_fixed_array_is_hoisted(self) -> None:
        cpp = _convert(WORKSPACE_F90)
        # The array moves onto a workspace struct, sized once.
        self.assertIn("struct ProcessWorkspace {", cpp)
        self.assertIn("fortran::Array<float, 1> buffer{{1000}};", cpp)
        # The routine takes the workspace and binds the array with auto&.
        self.assertIn(
            "void process(ProcessWorkspace& process_workspace", cpp
        )
        self.assertIn("auto& buffer = process_workspace.buffer;", cpp)
        # No per-call array declaration left in the body.
        self.assertNotIn("fortran::Array<float, 1> buffer{{1000}};\n\n  ", cpp)

    def test_workspace_allocated_once_in_caller(self) -> None:
        cpp = _convert(WORKSPACE_F90)
        # main allocates one workspace and passes it to both calls.
        self.assertIn("ProcessWorkspace process_workspace", cpp)
        self.assertEqual(cpp.count("process(process_workspace"), 2)

    def test_recursive_routine_keeps_per_call_array(self) -> None:
        cpp = _convert(RECURSIVE_F90)
        # No workspace for a recursive routine; the array stays local.
        self.assertNotIn("DescendWorkspace", cpp)
        self.assertIn("fortran::Array<float, 1> scratch{{100}};", cpp)

    def test_automatic_array_is_not_hoisted(self) -> None:
        cpp = _convert(AUTOMATIC_F90)
        # tmp(n) has a runtime bound, so it can't be sized once in a
        # workspace; it stays a per-call local.
        self.assertNotIn("WorkWorkspace", cpp)
        self.assertIn("fortran::Array<float, 1> tmp{{n}};", cpp)

    def test_no_static_or_thread_local(self) -> None:
        cpp = _convert(WORKSPACE_F90)
        self.assertNotIn("static ", cpp)
        self.assertNotIn("thread_local", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class WorkspaceRunTests(unittest.TestCase):
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

    def test_workspace_reuse_is_correct(self) -> None:
        out = self._run(WORKSPACE_F90)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # buffer(500) = 500*scale: 1000 then 1500.  (List-directed real
        # output uses C++'s default << formatting.)
        self.assertEqual(lines[0], "1000")
        self.assertEqual(lines[1], "1500")

    def test_recursion_each_level_independent(self) -> None:
        out = self._run(RECURSIVE_F90)
        lines = [l.split() for l in out.splitlines() if l.strip()]
        # Each level keeps its own scratch(1) == its depth: 0,1,2,3.
        depths = [int(parts[0]) for parts in lines]
        scratch = [float(parts[1]) for parts in lines]
        self.assertEqual(depths, [0, 1, 2, 3])
        self.assertEqual(scratch, [0.0, 1.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
