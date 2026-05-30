"""Tests for derived types (type ... end type) and component access."""

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


POINT_F90 = """\
program dt
  type :: point
    real :: x
    real :: y
  end type point

  type(point) :: p
  p%x = 1.0
  p%y = 2.0
  print *, p%x + p%y
end program
"""


VEC_PARAM_F90 = """\
module geom
  type :: vec2
    real :: x
    real :: y
  end type vec2
end module

subroutine scale(v, factor)
  use geom
  type(vec2), intent(inout) :: v
  real, intent(in) :: factor
  v%x = v%x * factor
  v%y = v%y * factor
end subroutine

program demo
  use geom
  type(vec2) :: p
  p%x = 3.0
  p%y = 4.0
  call scale(p, 2.0)
  print *, p%x, p%y
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
class DerivedTypeEmitTests(unittest.TestCase):
    def test_type_becomes_struct(self) -> None:
        cpp = _convert(POINT_F90)
        self.assertIn("struct Point {", cpp)
        self.assertIn("float x{};", cpp)
        self.assertIn("float y{};", cpp)

    def test_declaration_uses_struct_type(self) -> None:
        cpp = _convert(POINT_F90)
        self.assertIn("Point p{};", cpp)

    def test_component_access_uses_dot(self) -> None:
        cpp = _convert(POINT_F90)
        self.assertIn("p.x = 1.0f;", cpp)
        self.assertIn("p.y = 2.0f;", cpp)
        self.assertIn("p.x + p.y", cpp)

    def test_derived_type_parameter(self) -> None:
        cpp = _convert(VEC_PARAM_F90)
        self.assertIn("struct Vec2 {", cpp)
        # intent(inout) derived type -> reference parameter.
        self.assertIn("void scale(Vec2& v, const float& factor)", cpp)
        self.assertIn("v.x = v.x * factor;", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class DerivedTypeRunTests(unittest.TestCase):
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

    def test_point_runs(self) -> None:
        out = self._run(POINT_F90)
        self.assertIn("3", out)

    def test_vec_param_runs(self) -> None:
        out = self._run(VEC_PARAM_F90)
        # scale(p, 2): (3,4) -> (6,8)
        self.assertIn("6", out)
        self.assertIn("8", out)


if __name__ == "__main__":
    unittest.main()
