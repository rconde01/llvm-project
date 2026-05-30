"""Tests for do-while, select-case, cycle/exit, and single-line if."""

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


CONTROL_F90 = """\
program cf
  integer :: i, n
  i = 0
  do while (i < 5)
    i = i + 1
    if (i == 2) cycle
    if (i == 4) exit
    print *, i
  end do
  n = 2
  select case (n)
  case (1)
    print *, "one"
  case (2, 3)
    print *, "two or three"
  case default
    print *, "other"
  end select
end program
"""


RANGE_CASE_F90 = """\
program rc
  integer :: k
  do k = 1, 6
    select case (k)
    case (1:2)
      print *, k, "low"
    case (3:4)
      print *, k, "mid"
    case default
      print *, k, "high"
    end select
  end do
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
class ControlFlowEmitTests(unittest.TestCase):
    def test_do_while_emits_while(self) -> None:
        cpp = _convert(CONTROL_F90)
        self.assertIn("while (i < 5) {", cpp)

    def test_cycle_and_exit(self) -> None:
        cpp = _convert(CONTROL_F90)
        self.assertIn("continue;", cpp)
        self.assertIn("break;", cpp)

    def test_select_case_emits_if_chain(self) -> None:
        cpp = _convert(CONTROL_F90)
        self.assertIn("if (n == 1) {", cpp)
        self.assertIn("else if (n == 2 || n == 3) {", cpp)
        self.assertIn("} else {", cpp)

    def test_select_case_range(self) -> None:
        cpp = _convert(RANGE_CASE_F90)
        # Selector is the loop variable k (simple name) -> no temp.
        self.assertIn("k >= 1 && k <= 2", cpp)
        self.assertIn("k >= 3 && k <= 4", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ControlFlowRunTests(unittest.TestCase):
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

    def test_control_flow_runs(self) -> None:
        out = self._run(CONTROL_F90)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # do-while prints 1 and 3 (i==2 cycles, i==4 exits), then the
        # select-case prints "two or three".
        self.assertEqual(lines[0], "1")
        self.assertEqual(lines[1], "3")
        self.assertIn("two or three", lines[2])

    def test_range_case_runs(self) -> None:
        out = self._run(RANGE_CASE_F90)
        self.assertIn("low", out)
        self.assertIn("mid", out)
        self.assertIn("high", out)


if __name__ == "__main__":
    unittest.main()
