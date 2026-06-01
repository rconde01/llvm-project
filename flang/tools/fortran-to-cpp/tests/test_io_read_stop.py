"""Tests for READ (input) and STOP / ERROR STOP statements."""

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


READ_STOP_F90 = """\
program rd
  integer :: n
  real :: x
  read *, n
  read(*, *) x
  if (n < 0) stop
  if (n > 100) stop 1
  print *, n, x
  stop "done"
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
class ReadStopEmitTests(unittest.TestCase):
    def test_read_uses_cin(self) -> None:
        cpp = _convert(READ_STOP_F90)
        # List-directed READ routes through ``read_list_item`` so a
        # Fortran ``/`` terminator preserves the destination's current
        # value (C++11's ``>>`` zeros the target on failure).
        self.assertIn("fortran::io::read_list_item(std::cin, n)", cpp)
        self.assertIn("fortran::io::read_list_item(std::cin, x)", cpp)

    def test_bare_stop_exits_zero(self) -> None:
        cpp = _convert(READ_STOP_F90)
        self.assertIn("std::exit(0);", cpp)

    def test_stop_with_code(self) -> None:
        cpp = _convert(READ_STOP_F90)
        self.assertIn("std::exit(1);", cpp)

    def test_stop_with_message(self) -> None:
        cpp = _convert(READ_STOP_F90)
        self.assertIn('std::cerr << "done"', cpp)

    def test_single_line_if_stop(self) -> None:
        # ``if (n < 0) stop`` must route through the same action
        # dispatcher (regression: it used to emit a TODO).
        cpp = _convert(READ_STOP_F90)
        self.assertNotIn("does not yet translate StopStmt", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ReadStopRunTests(unittest.TestCase):
    def _run(self, src: str, stdin: str) -> tuple[str, int]:
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
            return run.stdout + run.stderr, run.returncode

    def test_reads_then_stops_with_message(self) -> None:
        out, code = self._run(READ_STOP_F90, "5\n3.5\n")
        self.assertIn("5", out)
        self.assertIn("3.5", out)
        self.assertIn("done", out)
        self.assertEqual(code, 0)

    def test_negative_input_stops_early(self) -> None:
        out, code = self._run(READ_STOP_F90, "-1\n0\n")
        # ``if (n < 0) stop`` exits 0 before printing.
        self.assertEqual(code, 0)
        self.assertNotIn("done", out)

    def test_large_input_stops_with_code_1(self) -> None:
        out, code = self._run(READ_STOP_F90, "200\n0\n")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
