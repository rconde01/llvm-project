"""Tests for numeric conversion intrinsics (INT/REAL/DBLE/NINT/...)."""

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


CONV_F90 = """\
program conv
  integer :: i
  real :: x
  real(kind=8) :: d
  i = 7
  x = real(i) / 2.0
  i = int(x)
  d = dble(i) + 0.5
  print *, x, i, nint(x), int(d)
end program
"""


KIND_F90 = """\
program k
  real :: x
  integer(kind=8) :: big
  x = 3.0
  big = int(x, 8)
  print *, real(big, 8)
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
class ConversionEmitTests(unittest.TestCase):
    def test_casts(self) -> None:
        cpp = _convert(CONV_F90)
        self.assertIn("static_cast<float>(i)", cpp)
        self.assertIn("static_cast<int32_t>(x)", cpp)
        self.assertIn("static_cast<double>(i)", cpp)

    def test_nint_uses_helper(self) -> None:
        cpp = _convert(CONV_F90)
        self.assertIn("ftn::nint(x)", cpp)

    def test_kind_selects_target_type(self) -> None:
        cpp = _convert(KIND_F90)
        # int(x, 8) -> int64; real(big, 8) -> double.
        self.assertIn("static_cast<int64_t>(x)", cpp)
        self.assertIn("static_cast<double>(big)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ConversionRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(CONV_F90)
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
            # x=3.5, int(x)=3, nint(3.5)=4, int(3.5)=3.
            self.assertEqual(run.stdout.split(), ["3.5", "3", "4", "3"])


if __name__ == "__main__":
    unittest.main()
