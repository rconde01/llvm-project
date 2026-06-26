"""Large constant array tables extract to a `static constexpr` table.

A PARAMETER array (immutable) becomes a `static constexpr` table plus a
zero-copy `const` view; a DATA array (writable) becomes the table plus a
one-line `assign_data` bulk copy.  Small constant lists stay inline as
`ftn::array_of(...)`.
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


# A PARAMETER array of 8 reals (== the extraction threshold) and a DATA
# array of 20 -- both above threshold.  A small 6-element DATA array stays
# inline.
TABLES_F90 = """\
program p
  real, parameter :: coef(8) = (/1.0,2.0,3.0,4.0,5.0,6.0,7.0,8.0/)
  integer :: small(6)
  real :: big(20)
  data small /1,2,3,4,5,6/
  data big /0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0, &
            1.1,1.2,1.3,1.4,1.5,1.6,1.7,1.8,1.9,2.0/
  print *, coef(1), small(2), big(15)
end program
"""


# PARAMETER with a non-default lower bound -> the view carries the static
# Lower NTTP.
LB_F90 = """\
program p
  double precision, parameter :: dc(0:9) = &
    (/0.1d0,0.2d0,0.3d0,0.4d0,0.5d0,0.6d0,0.7d0,0.8d0,0.9d0,1.0d0/)
  print *, dc(0), dc(9)
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
class ConstantTableEmitTests(unittest.TestCase):
    def test_parameter_array_becomes_constexpr_view(self) -> None:
        cpp = _convert(TABLES_F90)
        self.assertIn("static constexpr float coef_data[] = {", cpp)
        self.assertIn(
            "ftn::ArrayRef<const float, 1> coef(coef_data, {8});", cpp
        )
        # No heap-allocating array_of for the PARAMETER array.
        self.assertNotIn("coef = ftn::array_of", cpp)
        self.assertNotIn("Array<float, 1> coef", cpp)

    def test_large_data_array_uses_assign_data(self) -> None:
        cpp = _convert(TABLES_F90)
        self.assertIn("static constexpr float big_data[] = {", cpp)
        self.assertIn("big.assign_data(big_data);", cpp)
        # The giant inline array_of for big is gone.
        self.assertNotIn("big = ftn::array_of(0.1f", cpp)

    def test_small_data_array_stays_inline(self) -> None:
        cpp = _convert(TABLES_F90)
        # 6 elements < threshold 8 -> inline array_of, no table.
        self.assertIn("small = ftn::array_of(1, 2, 3, 4, 5, 6);", cpp)
        self.assertNotIn("small_data", cpp)

    def test_parameter_view_carries_static_lower(self) -> None:
        cpp = _convert(LB_F90)
        self.assertIn("static constexpr double dc_data[] = {", cpp)
        self.assertIn(
            "ftn::ArrayRef<const double, 1, std::array<ftn::index_t, 1>{0}> "
            "dc(dc_data,",
            cpp,
        )


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ConstantTableRunTests(unittest.TestCase):
    def _run(self, src: str) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(src)
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(src))
            exe = Path(d) / "out"
            cxx = (
                shutil.which("c++") or shutil.which("g++")
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
            return run.stdout.split()

    def test_tables_run(self) -> None:
        # coef(1)=1, small(2)=2, big(15)=1.5
        self.assertEqual(self._run(TABLES_F90), ["1", "2", "1.5"])

    def test_lb_table_runs(self) -> None:
        # dc(0)=0.1, dc(9)=1.0
        self.assertEqual(self._run(LB_F90), ["0.1", "1"])


if __name__ == "__main__":
    unittest.main()
