"""Tests for character intrinsics (trim/len/index/adjustl) and ``//``."""

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


CHARS_F90 = """\
program chars
  character(len=20) :: name, greeting
  name = "  World  "
  greeting = "Hello, " // trim(adjustl(name)) // "!"
  print *, trim(greeting)
  print *, len_trim(name), index(greeting, "World")
end program
"""


REPEAT_SCAN_F90 = """\
program rsv
  character(len=10) :: s
  integer :: n, v
  s = repeat('ab', 3)
  n = scan('hello', 'l')
  v = verify('hello', 'helo')
  print *, trim(s), n, v
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


def _compile_and_run(src: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        cpp = Path(d) / "out.cpp"
        cpp.write_text(convert_file_to_tmp(src))
        exe = Path(d) / "out"
        cxx = (
            shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        )
        assert cxx is not None
        comp = subprocess.run(
            [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE), str(cpp), "-o", str(exe)],
            capture_output=True, text=True, check=False,
        )
        if comp.returncode != 0:
            raise AssertionError(f"compile failed:\n{comp.stderr}\n{cpp.read_text()}")
        run = subprocess.run([str(exe)], capture_output=True, text=True, check=False)
        assert run.returncode == 0, run.stderr
        return run.stdout


def convert_file_to_tmp(src: str) -> str:
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
class CharacterEmitTests(unittest.TestCase):
    def test_concat_uses_runtime_helper(self) -> None:
        cpp = _convert(CHARS_F90)
        self.assertIn("fortran::concat(", cpp)

    def test_trim_adjustl_nested_args_not_duplicated(self) -> None:
        cpp = _convert(CHARS_F90)
        # Regression: nested calls must not flatten their arg lists.
        self.assertIn("fortran::trim(fortran::adjustl(name))", cpp)

    def test_intrinsics_mapped(self) -> None:
        cpp = _convert(CHARS_F90)
        self.assertIn("fortran::len_trim(name)", cpp)
        self.assertIn("fortran::index(greeting,", cpp)

    def test_repeat_scan_verify_mapped(self) -> None:
        cpp = _convert(REPEAT_SCAN_F90)
        self.assertIn('fortran::repeat("ab"sv, 3)', cpp)
        self.assertIn('fortran::scan("hello"sv, "l"sv)', cpp)
        self.assertIn('fortran::verify("hello"sv, "helo"sv)', cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class CharacterRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(CHARS_F90)
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
            lines = run.stdout.splitlines()
            self.assertIn("Hello, World!", lines[0])
            # len_trim("  World" padded) = 7; index of "World" = 8.
            self.assertEqual(lines[1].split(), ["7", "8"])

    def test_repeat_scan_verify_runs(self) -> None:
        out = _compile_and_run(REPEAT_SCAN_F90)
        # repeat('ab',3)="ababab"; scan('hello','l')=3; verify ok -> 0.
        self.assertEqual(out.split(), ["ababab", "3", "0"])


if __name__ == "__main__":
    unittest.main()
