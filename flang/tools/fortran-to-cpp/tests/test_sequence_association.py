"""Fortran sequence / storage association at call sites.

FORTRAN 77 lets an actual argument's storage be reinterpreted to match a
dummy of a different shape.  This file covers the *whole-array actual to a
scalar dummy* form: the dummy is storage-associated with the array's first
element, so the converter passes ``fortran::first(array)``.
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


@unittest.skipUnless(have_flang(), "flang binary not available")
class WholeArrayToScalarEmitTests(unittest.TestCase):
    def test_actual_passed_as_first_element(self) -> None:
        cpp = convert_project(WHOLE_ARRAY_TO_SCALAR_F, suffix=".f")
        self.assertIn("head(fortran::first(a))", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class WholeArrayToScalarRunTests(unittest.TestCase):
    def test_scalar_dummy_sees_first_element(self) -> None:
        # head(a) is storage-associated with a(1) == 7.
        self.assertEqual(int(run_project(WHOLE_ARRAY_TO_SCALAR_F, suffix=".f").strip()), 7)


if __name__ == "__main__":
    unittest.main()
