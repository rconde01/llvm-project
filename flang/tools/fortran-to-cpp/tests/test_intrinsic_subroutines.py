"""Tests for intrinsic subroutines invoked with CALL (CPU_TIME, SYSTEM_CLOCK)."""

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


TIMING_F90 = """\
program timing
  implicit none
  real :: t1, t2
  integer :: c1, c2, rate, cmax
  integer :: i
  real :: s
  call cpu_time(t1)
  call system_clock(c1, rate, cmax)
  s = 0.0
  do i = 1, 1000
    s = s + real(i)
  end do
  call system_clock(c2)
  call cpu_time(t2)
  if (t2 >= t1 .and. c2 >= c1 .and. rate == 1000 .and. s > 0.0) then
    print *, "ok"
  else
    print *, "bad"
  end if
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
class IntrinsicSubroutineEmitTests(unittest.TestCase):
    def test_calls_map_to_runtime(self) -> None:
        cpp = _convert(TIMING_F90)
        self.assertIn("ftn::cpu_time(t1);", cpp)
        self.assertIn("ftn::system_clock(c1, rate, cmax);", cpp)
        self.assertIn("ftn::system_clock(c2);", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class IntrinsicSubroutineRunTests(unittest.TestCase):
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

    def test_timing_runs(self) -> None:
        self.assertIn("ok", self._run(TIMING_F90))


if __name__ == "__main__":
    unittest.main()
