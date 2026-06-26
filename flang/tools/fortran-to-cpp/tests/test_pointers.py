"""Tests for POINTER / TARGET and pointer assignment (=>)."""

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


SCALAR_PTR_F90 = """\
program ptr
  real, target :: x, y
  real, pointer :: p
  x = 1.0
  y = 2.0
  p => x
  p = 99.0
  print *, x, associated(p)
  p => y
  print *, p
end program
"""


ARRAY_PTR_F90 = """\
program aptr
  real, target :: a(10)
  real, pointer :: p(:)
  integer :: i
  do i = 1, 10
    a(i) = i
  end do
  p => a(2:8:2)
  print *, p(1), p(4), associated(p)
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
class PointerEmitTests(unittest.TestCase):
    def test_scalar_pointer_is_raw_pointer(self) -> None:
        cpp = _convert(SCALAR_PTR_F90)
        self.assertIn("float* p = nullptr;", cpp)
        self.assertIn("p = &x;", cpp)        # => takes address
        self.assertIn("(*p) = 99.0f;", cpp)  # value assign derefs
        self.assertIn("ftn::associated(p)", cpp)

    def test_array_pointer_is_arrayref(self) -> None:
        cpp = _convert(ARRAY_PTR_F90)
        self.assertIn("ftn::ArrayRef<float, 1> p;", cpp)
        # POINTER assignment ``p => target`` rebinds the view; element-wise
        # ``=`` would copy data instead (the IRI read_data_SD pattern).
        self.assertIn("p.rebind(a.section(2, 8, 2));", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class PointerRunTests(unittest.TestCase):
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

    def test_scalar_pointer_runs(self) -> None:
        lines = self._run(SCALAR_PTR_F90).splitlines()
        # p=>x; *p=99 -> x=99; associated true; then p=>y -> *p=2.
        self.assertEqual(lines[0].split()[0], "99")
        self.assertEqual(lines[1].strip(), "2")

    def test_array_pointer_runs(self) -> None:
        out = self._run(ARRAY_PTR_F90)
        # a(2:8:2) = [2,4,6,8]; p(1)=2, p(4)=8.
        parts = out.split()
        self.assertEqual(parts[0], "2")
        self.assertEqual(parts[1], "8")


if __name__ == "__main__":
    unittest.main()
