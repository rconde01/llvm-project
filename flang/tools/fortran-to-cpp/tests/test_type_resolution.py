"""Tests for dumper-based type resolution.

flang resolves every variable's type (kind, custom IMPLICIT, star-kinds,
host/use association) in its symbol table; the JSON dumper now emits that
on each Name as ``type`` / ``rank``, and the converter uses it instead of
re-deriving types.  This fixes cases the parse-tree spelling alone got
wrong (``integer*8``) or couldn't know (``implicit double precision``).
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


DP_F = """\
      program ty
      implicit double precision (a-h,o-z)
      dimension d(5)
      integer*8 big
      x = 1.5
      n = 3
      d(1) = x
      big = 10
      print *, x, n, d(1), big
      end
"""


def _convert(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".f", delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


@unittest.skipUnless(_have_flang(), "flang binary not available")
class TypeResolutionEmitTests(unittest.TestCase):
    def test_implicit_double_precision(self) -> None:
        cpp = _convert(DP_F)
        # implicit double precision (a-h,o-z): x and d are double.
        self.assertIn("double x{};", cpp)
        self.assertIn("fortran::Array<double, 1> d{{5}};", cpp)

    def test_in_letters_still_integer(self) -> None:
        cpp = _convert(DP_F)
        # n falls under the default i-n integer rule (not overridden).
        self.assertIn("std::int32_t n{};", cpp)

    def test_star_kind_explicit_decl(self) -> None:
        cpp = _convert(DP_F)
        # integer*8 -> 64-bit, resolved from the symbol table.
        self.assertIn("std::int64_t big{};", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class TypeResolutionRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(DP_F))
            exe = Path(d) / "out"
            cxx = (
                shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
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
            # x=1.5, n=3, d(1)=1.5, big=10.
            self.assertEqual(run.stdout.split(), ["1.5", "3", "1.5", "10"])


if __name__ == "__main__":
    unittest.main()
