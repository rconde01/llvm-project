"""Tests for ASSOCIATE and BLOCK constructs."""

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


AS_F90 = """\
program as
  real :: x, y
  x = 3.0
  y = 4.0
  associate (h => sqrt(x*x + y*y))
    print *, h
  end associate
  block
    integer :: tmp
    tmp = 42
    print *, tmp
  end block
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
class BlockAssociateEmitTests(unittest.TestCase):
    def test_associate_binds_full_selector(self) -> None:
        cpp = _convert(AS_F90)
        # Regression: the whole sqrt(...) selector, not just its arg.
        self.assertIn("auto&& h = ftn::sqrt(x * x + y * y);", cpp)

    def test_block_has_scoped_local(self) -> None:
        cpp = _convert(AS_F90)
        self.assertIn("int32_t tmp{};", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class BlockAssociateRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(AS_F90)
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
            # h = sqrt(9+16) = 5; block prints 42.
            self.assertEqual(run.stdout.split(), ["5", "42"])


if __name__ == "__main__":
    unittest.main()
