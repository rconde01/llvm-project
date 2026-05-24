"""Tests for allocatable arrays (allocate / deallocate)."""

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


ALLOC_F90 = """\
program al
  real, allocatable :: a(:)
  integer :: n, i
  n = 5
  allocate(a(n))
  do i = 1, n
    a(i) = i * 2.0
  end do
  print *, a(3)
  deallocate(a)
end program
"""


ALLOC_LOWER_F90 = """\
program al
  integer, allocatable :: a(:)
  integer :: i
  allocate(a(0:4))
  do i = 0, 4
    a(i) = i * i
  end do
  print *, a(0), a(4)
  deallocate(a)
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
class AllocatableEmitTests(unittest.TestCase):
    def test_declaration_is_empty_array(self) -> None:
        cpp = _convert(ALLOC_F90)
        # Deferred-shape decl: default-constructed (no extents).
        self.assertIn("fortran::Array<float, 1> a;", cpp)

    def test_allocate_move_assigns_sized_array(self) -> None:
        cpp = _convert(ALLOC_F90)
        self.assertIn("a = fortran::Array<float, 1>({n});", cpp)

    def test_deallocate_calls_method(self) -> None:
        cpp = _convert(ALLOC_F90)
        self.assertIn("a.deallocate();", cpp)

    def test_allocate_with_lower_bound(self) -> None:
        cpp = _convert(ALLOC_LOWER_F90)
        # allocate(a(0:4)) -> (lower, extent) ctor form.
        self.assertIn("a = fortran::Array<std::int32_t, 1>({0}, {", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class AllocatableRunTests(unittest.TestCase):
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

    def test_alloc_runs(self) -> None:
        out = self._run(ALLOC_F90)
        # a(3) = 3 * 2.0 = 6
        self.assertIn("6", out)

    def test_alloc_lower_bound_runs(self) -> None:
        out = self._run(ALLOC_LOWER_F90)
        # a(0)=0, a(4)=16
        parts = out.split()
        self.assertEqual(parts, ["0", "16"])


if __name__ == "__main__":
    unittest.main()
