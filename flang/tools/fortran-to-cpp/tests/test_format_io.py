"""Tests for formatted I/O: format parsing and emitted output."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from converter import convert_file
from converter.format import FormatParseError, render_format


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


def _have_cxx() -> bool:
    return any(shutil.which(n) for n in ("c++", "g++", "clang++"))


RUNTIME_INCLUDE = Path(__file__).resolve().parent.parent / "runtime" / "include"


class FormatParserTests(unittest.TestCase):
    """Pure-Python tests for the format -> C++ chunk mapping."""

    def test_integer_descriptor(self) -> None:
        chunks, nl = render_format("(I5)", ["n"])
        self.assertEqual(chunks, ['std::format("{:5d}", n)'])
        self.assertFalse(nl)

    def test_fixed_float(self) -> None:
        chunks, _ = render_format("(F8.2)", ["x"])
        self.assertEqual(chunks, ['std::format("{:#8.2f}", x)'])

    def test_mixed_with_spacing(self) -> None:
        chunks, _ = render_format("(I5, 1X, F8.2)", ["n", "x"])
        self.assertEqual(
            chunks,
            [
                'std::format("{:5d}", n)',
                '" "sv',
                'std::format("{:#8.2f}", x)',
            ],
        )

    def test_literal_text(self) -> None:
        chunks, _ = render_format("('value=', I3)", ["n"])
        self.assertEqual(
            chunks, ['"value="sv', 'std::format("{:3d}", n)']
        )

    def test_e_descriptor_uses_runtime_helper(self) -> None:
        chunks, _ = render_format("(E12.4)", ["x"])
        self.assertEqual(chunks, ["fortran::io::fmt_E(x, 12, 4)"])

    def test_repeat_count(self) -> None:
        chunks, _ = render_format("(3I4)", ["a", "b", "c"])
        self.assertEqual(
            chunks,
            [
                'std::format("{:4d}", a)',
                'std::format("{:4d}", b)',
                'std::format("{:4d}", c)',
            ],
        )

    def test_slash_is_newline(self) -> None:
        chunks, _ = render_format("(I3, /, I3)", ["a", "b"])
        self.assertEqual(
            chunks,
            [
                'std::format("{:3d}", a)',
                "'\\n'",
                'std::format("{:3d}", b)',
            ],
        )

    def test_repeated_group_expands(self) -> None:
        # ``2(I3, F5.1)`` repeats the inner group for two (a,b) and (c,d).
        chunks, _ = render_format("(2(I3, F5.1))", ["a", "b", "c", "d"])
        self.assertEqual(
            chunks,
            [
                'std::format("{:3d}", a)',
                'std::format("{:#5.1f}", b)',
                'std::format("{:3d}", c)',
                'std::format("{:#5.1f}", d)',
            ],
        )

    def test_p_scale_factor(self) -> None:
        # ``1PE12.2`` applies a scale factor via the runtime helper.
        chunks, _ = render_format("(1PE12.2)", ["x"])
        self.assertEqual(
            chunks,
            ["fortran::io::fmt_E_with_scale(x, 1, 12, 2)"],
        )

    def test_slash_alt(self) -> None:
        chunks, _ = render_format("(I3/I3)", ["a", "b"])
        self.assertEqual(
            chunks,
            ['std::format("{:3d}", a)', "'\\n'", 'std::format("{:3d}", b)'],
        )

    def test_dollar_suppresses_trailing_newline(self) -> None:
        # ``$`` is the non-standard prompt marker -- caller must omit
        # the trailing newline.
        chunks, nl = render_format("('prompt: ', $)", [])
        self.assertEqual(chunks, ['"prompt: "sv'])
        self.assertTrue(nl)


IO_F90 = """\
program io
  integer :: n
  real :: x
  n = 42
  x = 3.14159
  print '(I5)', n
  print '(F10.4)', x
  write(*, '(I5, 1X, F8.2)') n, x
  print '(E12.4)', x
end program
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class FormatEmitTests(unittest.TestCase):
    def _convert(self, src: str) -> str:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(src)
            tmp = Path(f.name)
        try:
            return convert_file(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def test_inline_std_format_for_clean_descriptors(self) -> None:
        cpp = self._convert(IO_F90)
        self.assertIn('std::format("{:5d}", n)', cpp)
        self.assertIn('std::format("{:#10.4f}", x)', cpp)

    def test_runtime_helper_for_e(self) -> None:
        cpp = self._convert(IO_F90)
        self.assertIn("fortran::io::fmt_E(x, 12, 4)", cpp)

    def test_write_to_star_uses_cout(self) -> None:
        cpp = self._convert(IO_F90)
        self.assertIn("std::cout <<", cpp)

    def test_runtime_format_routes_to_interpreter(self) -> None:
        # A FORMAT held in a CHARACTER variable is not a compile-time
        # constant, so it must route through the runtime interpreter
        # rather than silently degrade to list-directed output.
        cpp = self._convert(RUNTIME_FMT_F)
        self.assertIn("fortran::io::format_record(fmt, x)", cpp)


# The format lives in a CHARACTER variable, so flang reports it as a
# (non-constant) ``variable`` -- the converter can't parse it at
# translation time and emits a runtime-interpreter call.
RUNTIME_FMT_F = """\
      program p
      character*8     fmt
      character*30    out
      double precision x
      x = 3.14159d0
      fmt = '(F20.13)'
      write (out, fmt) x
      print *, out
      end
"""


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class FormatRunTests(unittest.TestCase):
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

    def test_runtime_format_output(self) -> None:
        # The runtime interpreter must format ``(F20.13)`` identically to
        # the compile-time path: 20-wide field, 13 fractional digits.
        out = self._run(RUNTIME_FMT_F)
        self.assertIn("3.1415900000000", out)

    def test_formatted_output_widths(self) -> None:
        out = self._run(IO_F90)
        lines = out.splitlines()
        # I5 of 42 -> "   42"
        self.assertEqual(lines[0], "   42")
        # F10.4 of 3.14159 -> "    3.1416"
        self.assertEqual(lines[1], "    3.1416")
        # I5,1X,F8.2 -> "   42     3.14"
        self.assertEqual(lines[2], "   42     3.14")
        # E12.4 -> "  0.3142E+01"
        self.assertEqual(lines[3], "  0.3142E+01")


if __name__ == "__main__":
    unittest.main()
