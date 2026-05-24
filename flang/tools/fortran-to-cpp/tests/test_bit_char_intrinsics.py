"""Tests for bit-manipulation and character<->integer intrinsics, plus
variadic MAX / MIN."""

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


INTR_F90 = """\
program p5
  implicit none
  integer :: i, j
  character :: c
  i = 65
  c = achar(i)
  j = iachar('B')
  print *, c, j
  print *, iand(12, 10), ior(12, 10), ishft(1, 3)
  print *, max(1, 2, 3), min(4, 2)
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
class IntrinsicEmitTests(unittest.TestCase):
    def test_char_intrinsics_mapped(self) -> None:
        cpp = _convert(INTR_F90)
        self.assertIn("fortran::achar(i)", cpp)
        self.assertIn('fortran::ichar("B"sv)', cpp)

    def test_bit_intrinsics_mapped(self) -> None:
        cpp = _convert(INTR_F90)
        self.assertIn("fortran::iand(12, 10)", cpp)
        self.assertIn("fortran::ior(12, 10)", cpp)
        self.assertIn("fortran::ishft(1, 3)", cpp)

    def test_variadic_max_uses_initializer_list(self) -> None:
        cpp = _convert(INTR_F90)
        # 3-arg max must use the braced form; 2-arg min stays plain.
        self.assertIn("std::max({1, 2, 3})", cpp)
        self.assertIn("std::min(4, 2)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class IntrinsicRunTests(unittest.TestCase):
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

    def test_intrinsics_run(self) -> None:
        lines = self._run(INTR_F90).splitlines()
        self.assertEqual(lines[0].split(), ["A", "66"])
        self.assertEqual(lines[1].split(), ["8", "14", "8"])
        self.assertEqual(lines[2].split(), ["3", "2"])


if __name__ == "__main__":
    unittest.main()
