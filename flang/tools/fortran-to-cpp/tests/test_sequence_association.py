"""Fortran sequence / storage association at call sites.

FORTRAN 77 lets an actual argument's storage be reinterpreted to match a
dummy of a different shape.  This file covers the *whole-array actual to a
scalar dummy* form: the dummy is storage-associated with the array's first
element, so the converter passes ``ftn::first(array)``.
"""

from __future__ import annotations

import unittest

from _support import convert_project, have_cxx, have_flang, run_project


WHOLE_ARRAY_TO_SCALAR_F = """\
      integer function head(n)
      integer n
      head = n
      end

      program p
      integer a(3)
      integer head
      external head
      a(1) = 7
      a(2) = 8
      a(3) = 9
      print *, head(a)
      end
"""


# A rank-1 actual passed to a 2-D explicit-shape dummy whose extents are
# *other dummies* (``V(NR,NC)``) -- the SPICE CORTAB ``VALUES(NCOLS,N)``
# pattern.  The seq_assoc reshape must spell the extents using the actual
# arguments passed for NR/NC at this call site (2 and 3), not the callee's
# dummy names (which don't exist in the caller).
SEQ_ASSOC_DUMMY_BOUNDS_F = """\
      subroutine fill(nr, nc, v)
      integer nr, nc
      double precision v(nr, nc)
      v(1, 1)   = 1.5
      v(nr, nc) = 9.5
      end

      program p
      double precision a(6)
      call fill(2, 3, a)
      print *, a(1), a(6)
      end
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class WholeArrayToScalarEmitTests(unittest.TestCase):
    def test_actual_passed_as_first_element(self) -> None:
        cpp = convert_project(WHOLE_ARRAY_TO_SCALAR_F, suffix=".f")
        self.assertIn("head(ftn::first(a))", cpp)

    def test_dummy_bounds_use_caller_actuals(self) -> None:
        cpp = convert_project(SEQ_ASSOC_DUMMY_BOUNDS_F, suffix=".f")
        # Extents NR, NC become the actual arguments 2 and 3 -- not the
        # callee's dummy names.
        self.assertIn("ftn::seq_assoc<2>(a, {1, 1}, {(2), (3)})", cpp)
        self.assertNotIn("{nr, nc}", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class WholeArrayToScalarRunTests(unittest.TestCase):
    def test_scalar_dummy_sees_first_element(self) -> None:
        # head(a) is storage-associated with a(1) == 7.
        self.assertEqual(int(run_project(WHOLE_ARRAY_TO_SCALAR_F, suffix=".f").strip()), 7)

    def test_dummy_bounds_reshape_runs(self) -> None:
        # a(6) views as V(2,3); V(1,1)->a(1)=1.5, V(2,3)->a(6)=9.5
        # (column-major: (2,3) -> offset 1+2*2 = 5 -> a(6)).
        self.assertEqual(
            run_project(SEQ_ASSOC_DUMMY_BOUNDS_F, suffix=".f").split(),
            ["1.5", "9.5"],
        )


if __name__ == "__main__":
    unittest.main()
