"""Tests for the SPREAD and EOSHIFT array intrinsics."""

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


SPREAD_F90 = """\
program p4
  implicit none
  integer :: v(3), m(2,3)
  v = [1, 2, 3]
  m = spread(v, 1, 2)
  print *, m(1,2), m(2,3)
  print *, eoshift(v, 1)
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
class SpreadEoshiftEmitTests(unittest.TestCase):
    def test_spread_is_whole_assigned_not_elementwise(self) -> None:
        cpp = _convert(SPREAD_F90)
        # spread returns a rank-2 array; it must stay a whole-array
        # assignment, not be elementwise-expanded into a loop.
        self.assertIn("m = ftn::spread(v, 1, 2);", cpp)

    def test_eoshift_mapped(self) -> None:
        cpp = _convert(SPREAD_F90)
        self.assertIn("ftn::eoshift(v, 1)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class SpreadEoshiftRunTests(unittest.TestCase):
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

    def test_spread_eoshift_run(self) -> None:
        lines = self._run(SPREAD_F90).splitlines()
        # spread(v,1,2): m(1,2)=v(2)=2, m(2,3)=v(3)=3.
        self.assertEqual(lines[0].split(), ["2", "3"])
        # eoshift([1,2,3], 1) -> [2, 3, 0].
        self.assertEqual(lines[1].split(), ["2", "3", "0"])


if __name__ == "__main__":
    unittest.main()
