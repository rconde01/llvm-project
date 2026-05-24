"""Tests for PACK / CSHIFT and elementwise array operators (masks)."""

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


PK_F90 = """\
program pk
  integer :: a(5), b(5), c(5)
  integer :: i
  do i = 1, 5
    a(i) = i
  end do
  b = cshift(a, 2)
  c = pack(a, a > 2)
  print *, b(1), b(4), c(1), c(3)
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
class PackCshiftEmitTests(unittest.TestCase):
    def test_calls_mapped_and_move_assigned(self) -> None:
        cpp = _convert(PK_F90)
        self.assertIn("b = fortran::cshift(a, 2);", cpp)
        # Inline array-relational mask passed as a value (a > 2).
        self.assertIn("fortran::pack(a, a > 2)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class PackCshiftRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(PK_F90)
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
            # a=[1..5]; cshift(a,2): b(1)=a(3)=3, b(4)=a(1)=1;
            # pack(a, a>2)=[3,4,5]: c(1)=3, c(3)=5.
            self.assertEqual(run.stdout.split(), ["3", "1", "3", "5"])


if __name__ == "__main__":
    unittest.main()
