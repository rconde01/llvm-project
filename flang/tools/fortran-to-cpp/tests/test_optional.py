"""Tests for OPTIONAL dummy arguments and PRESENT."""

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


OPT_F90 = """\
module m
contains
  subroutine greet(name, times)
    integer, intent(in) :: name
    integer, intent(in), optional :: times
    integer :: n
    n = 1
    if (present(times)) n = times
    print *, name, n
  end subroutine
end module

program demo
  use m
  call greet(7)
  call greet(7, 3)
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
class OptionalEmitTests(unittest.TestCase):
    def test_optional_param_is_std_optional_with_default(self) -> None:
        cpp = _convert(OPT_F90)
        self.assertIn(
            "std::optional<std::int32_t> times = std::nullopt", cpp
        )

    def test_present_uses_has_value(self) -> None:
        cpp = _convert(OPT_F90)
        self.assertIn("times.has_value()", cpp)

    def test_value_use_derefs(self) -> None:
        cpp = _convert(OPT_F90)
        self.assertIn("n = times.value();", cpp)

    def test_call_sites(self) -> None:
        cpp = _convert(OPT_F90)
        self.assertIn("greet(7);", cpp)
        self.assertIn("greet(7, 3);", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class OptionalRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(OPT_F90)
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
            # greet(7): times absent -> n=1; greet(7,3): n=3.
            self.assertEqual(lines[0].split(), ["7", "1"])
            self.assertEqual(lines[1].split(), ["7", "3"])


# An optional argument followed by a non-optional one — C++ forbids a
# defaulted parameter before a non-defaulted one, and Fortran keyword
# calls can leave gaps, so the converter must place defaults only on the
# trailing optional run and fill omitted optionals with std::nullopt.
INTERLEAVED_F90 = """\
module m
contains
  subroutine s(a, b, c)
    integer, intent(in), optional :: a
    real, intent(in) :: b(3)
    integer, intent(in), optional :: c
    if (present(a)) print *, a
    print *, int(b(1))
    if (present(c)) print *, c
  end subroutine
end module
program p
  use m
  real :: arr(3)
  arr = 1.0
  call s(b=arr)
  call s(5, arr)
end program
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class OptionalOrderingTests(unittest.TestCase):
    def test_default_only_on_trailing_optional(self) -> None:
        cpp = _convert(INTERLEAVED_F90)
        # ``a`` precedes the non-optional array ``b`` -> no default;
        # trailing ``c`` keeps its default.
        self.assertIn("std::optional<std::int32_t> a,", cpp)
        self.assertIn("std::optional<std::int32_t> c = std::nullopt", cpp)
        self.assertNotIn("std::optional<std::int32_t> a = std::nullopt", cpp)

    def test_omitted_optional_filled_with_nullopt(self) -> None:
        cpp = _convert(INTERLEAVED_F90)
        # call s(b=arr): a omitted (non-trailing) -> explicit nullopt;
        # c omitted (trailing) -> dropped.
        self.assertIn("s(std::nullopt, arr);", cpp)
        self.assertIn("s(5, arr);", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class OptionalOrderingRunTests(unittest.TestCase):
    def test_compiles_and_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(INTERLEAVED_F90)
            cpp = Path(d) / "out.cpp"
            cpp.write_text(convert_file(f))
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
            # s(b=arr): a absent, b(1)=1; s(5,arr): a=5, b(1)=1.
            self.assertEqual(run.stdout.split(), ["1", "5", "1"])


if __name__ == "__main__":
    unittest.main()
