"""Tests for DO CONCURRENT (lowered to plain / nested for loops)."""

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


DC_F90 = """\
program dc
  integer :: a(5), i
  do concurrent (i = 1:5)
    a(i) = i * i
  end do
  print *, a(1), a(5)
end program
"""


DC2_F90 = """\
program dc2
  integer :: m(2,3), i, j
  do concurrent (i = 1:2, j = 1:3)
    m(i,j) = i*10 + j
  end do
  print *, m(1,1), m(2,3)
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
class DoConcurrentEmitTests(unittest.TestCase):
    def test_single_index(self) -> None:
        cpp = _convert(DC_F90)
        self.assertIn("for (fortran::index_t i = 1; i <= 5; ++i)", cpp)

    def test_multi_index_is_nested(self) -> None:
        cpp = _convert(DC2_F90)
        self.assertIn("for (fortran::index_t i = 1; i <= 2; ++i)", cpp)
        self.assertIn("for (fortran::index_t j = 1; j <= 3; ++j)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class DoConcurrentRunTests(unittest.TestCase):
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

    def test_single(self) -> None:
        self.assertEqual(self._run(DC_F90).split(), ["1", "25"])

    def test_multi(self) -> None:
        self.assertEqual(self._run(DC2_F90).split(), ["11", "23"])


if __name__ == "__main__":
    unittest.main()
