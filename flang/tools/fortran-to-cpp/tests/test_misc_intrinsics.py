"""Tests for mod/modulo/merge/maxloc/minloc."""

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


MISC_F90 = """\
program misc
  integer :: a(5)
  integer :: i, r, m
  do i = 1, 5
    a(i) = mod(i*7, 5)
  end do
  r = modulo(-7, 3)
  m = maxloc(a, 1)
  print *, a(1), a(2), r, m, merge(10, 20, a(1) > 0)
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
class MiscIntrinsicEmitTests(unittest.TestCase):
    def test_integer_mod_uses_runtime_helper(self) -> None:
        cpp = _convert(MISC_F90)
        # Not std::fmod (which would be wrong for integers).
        self.assertIn("ftn::mod(", cpp)
        self.assertNotIn("std::fmod", cpp)

    def test_others_mapped(self) -> None:
        cpp = _convert(MISC_F90)
        self.assertIn("ftn::modulo(", cpp)
        self.assertIn("ftn::maxloc(a, 1)", cpp)
        self.assertIn("ftn::merge(", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class MiscIntrinsicRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(MISC_F90)
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
            # a=[2,4,1,3,0]; modulo(-7,3)=2; maxloc=2; merge=10.
            self.assertEqual(run.stdout.split(), ["2", "4", "2", "2", "10"])


if __name__ == "__main__":
    unittest.main()
