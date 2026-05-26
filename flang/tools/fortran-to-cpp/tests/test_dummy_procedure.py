"""Dummy procedures — a function passed as an argument and called inside.

``EXTERNAL f`` marks ``f`` a procedure dummy; the converter gives it a
``std::function`` parameter and, at the call site, wraps the actual
routine in a lambda (so any threaded state is captured).
"""

from __future__ import annotations

import unittest

from _support import convert, have_cxx, have_flang, run


DUMMY_PROC_F90 = """\
real function apply_twice(f, x)
  real :: f, x
  external f
  apply_twice = f(x) + f(x)
end function

real function square(x)
  real :: x
  square = x * x
end function

program p
  real :: r
  external square
  r = apply_twice(square, 3.0)
  print *, r
end program
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class DummyProcedureEmitTests(unittest.TestCase):
    def test_dummy_is_std_function_param(self) -> None:
        cpp = convert(DUMMY_PROC_F90)
        self.assertIn("std::function<float(float)>", cpp)

    def test_actual_is_wrapped_in_a_lambda(self) -> None:
        cpp = convert(DUMMY_PROC_F90)
        self.assertIn("apply_twice([", cpp)  # square passed as a lambda


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class DummyProcedureRunTests(unittest.TestCase):
    def test_calls_the_passed_function(self) -> None:
        # square(3) + square(3) = 9 + 9 = 18
        self.assertEqual(float(run(DUMMY_PROC_F90).strip()), 18.0)


if __name__ == "__main__":
    unittest.main()
