"""Tests for WHERE constructs (masked array assignment)."""

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


WHERE_F90 = """\
program wh
  real :: a(5), b(5)
  integer :: i
  do i = 1, 5
    a(i) = i - 3
  end do
  where (a > 0.0)
    b = a
  elsewhere
    b = -a
  end where
  where (a == 0.0) b = 99.0
  print *, b(1), b(3), b(5)
end program
"""


# WHERE whose mask and target are array *sections*, not whole arrays
# (``where (.not. m(0:k)) p(0:k) = 0`` -- the NRLMSIS2 idiom).  Lowered to a
# single rank-1 position loop with a masked ``if``; both sections index at the
# same counter so differing lower bounds line up.
WHERE_SECTION_F90 = """\
program whs
  real :: p(0:9)
  logical :: m(0:9)
  integer :: i
  do i = 0, 9
    p(i) = 1.0
    m(i) = (mod(i, 2) == 0)
  end do
  where (.not. m(0:5)) p(0:5) = 0.0
  print *, p(0), p(1), p(2), p(5), p(6)
end program
"""


# WHERE whose target is a *module* array (host-associated, not a local) --
# the array-expansion pass only sees locals, so the target's shape comes from
# flang's resolved type instead.  Previously a hard "no whole-array target".
WHERE_MODULE_F90 = """\
module m
  integer, parameter :: n = 4
  logical :: flag(n) = .true.
contains
  subroutine apply(mask)
    logical, intent(in) :: mask(n)
    where (mask) flag = .false.
  end subroutine
end module
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
class WhereEmitTests(unittest.TestCase):
    def test_construct_is_masked_loop(self) -> None:
        cpp = _convert(WHERE_F90)
        self.assertRegex(cpp, r"for \(ftn::index_t _i\d = b\.lbound")
        self.assertRegex(cpp, r"if \(a\(_i\d\) > 0\.0f\) \{")
        self.assertRegex(cpp, r"b\(_i\d\) = a\(_i\d\);")
        self.assertIn("} else {", cpp)
        self.assertRegex(cpp, r"b\(_i\d\) = -a\(_i\d\);")

    def test_single_line_where(self) -> None:
        cpp = _convert(WHERE_F90)
        self.assertRegex(cpp, r"if \(a\(_i\d\) == 0\.0f\) \{")
        self.assertRegex(cpp, r"b\(_i\d\) = 99\.0f;")

    def test_section_target_is_position_loop(self) -> None:
        # No whole-array name target -> rank-1 ``_k`` position loop; mask and
        # target sections both index at the same counter.
        cpp = _convert(WHERE_SECTION_F90)
        self.assertRegex(cpp, r"for \(ftn::index_t _k\d = 0;")
        self.assertRegex(cpp, r"if \(!m\(0 \+ _k\d\)\)")
        self.assertRegex(cpp, r"p\(0 \+ _k\d\) = 0\.0f;")

    def test_module_array_target_resolved_from_flang_shape(self) -> None:
        # ``flag`` is a module array (not a local); its shape comes from the
        # resolved type so the WHERE still expands instead of erroring.
        cpp = _convert(WHERE_MODULE_F90)
        self.assertRegex(cpp, r"for \(ftn::index_t _i\d = flag\.lbound")
        self.assertRegex(cpp, r"if \(mask\(_i\d\)\)")
        self.assertRegex(cpp, r"flag\(_i\d\) = false;")


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class WhereRunTests(unittest.TestCase):
    def test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(WHERE_F90)
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
            # a=[-2,-1,0,1,2]; where>0 b=a else -a -> [2,1,0,1,2];
            # where==0 b=99 -> b(3)=99.  print b(1),b(3),b(5).
            self.assertEqual(run.stdout.split(), ["2", "99", "2"])

    def test_section_where_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(WHERE_SECTION_F90)
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
            # m true at even i; .not.m true at odd -> p(odd<=5)=0.  p(6)
            # untouched (outside 0:5).  print p(0),p(1),p(2),p(5),p(6).
            self.assertEqual(run.stdout.split(), ["1", "0", "1", "0", "1"])


if __name__ == "__main__":
    unittest.main()
