"""Tests for implied-do loops in array constructors and I/O."""

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


ID_F90 = """\
program id
  integer :: a(5), i, n
  n = 5
  a = [(i*i, i=1,5)]
  print *, (a(i), i=1,n)
end program
"""


READ_ID_F90 = """\
program rid
  integer :: a(3), i
  read *, (a(i), i=1,3)
  print *, a(1), a(3)
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
class ImpliedDoEmitTests(unittest.TestCase):
    def test_constructor_implied_do_is_fill_loop(self) -> None:
        cpp = _convert(ID_F90)
        self.assertIn("for (fortran::index_t i = 1; i <= 5; ++i)", cpp)
        self.assertRegex(cpp, r"a\(a\.lbound\(1\) \+ i - 1\) = i \* i;")

    def test_io_implied_do_is_loop(self) -> None:
        cpp = _convert(ID_F90)
        self.assertIn("for (fortran::index_t i = 1; i <= n; ++i)", cpp)
        self.assertIn("std::cout << a(i) << ' ';", cpp)

    def test_read_implied_do_is_loop(self) -> None:
        cpp = _convert(READ_ID_F90)
        self.assertIn("std::cin >> a(i);", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ImpliedDoRunTests(unittest.TestCase):
    def _run(self, src: str, stdin: str = "") -> str:
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
                [str(exe)], input=stdin, capture_output=True, text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            return run.stdout

    def test_constructor_and_output(self) -> None:
        out = self._run(ID_F90)
        self.assertEqual(out.split(), ["1", "4", "9", "16", "25"])

    def test_read_implied_do(self) -> None:
        out = self._run(READ_ID_F90, stdin="11 22 33\n")
        self.assertEqual(out.split(), ["11", "33"])


if __name__ == "__main__":
    unittest.main()
