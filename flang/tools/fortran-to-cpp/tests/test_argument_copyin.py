"""Argument copy-in — a constant/expression passed to a modifiable dummy.

Fortran lets any expression be an actual argument; for an INOUT dummy it
binds a temporary (the write-back is discarded).  C++ can't bind a
non-const ``T&`` to an rvalue, so the converter routes such actuals
through ``fortran::byref``, which materializes the value for the call.
"""

from __future__ import annotations

import unittest

from _support import convert, have_cxx, have_flang, run


COPYIN_F90 = """\
subroutine add_one(x)
  real :: x
  x = x + 1.0
  print *, x
end subroutine

program p
  call add_one(2.0 + 3.0)
end program
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class ArgumentCopyinEmitTests(unittest.TestCase):
    def test_expression_actual_wrapped_in_byref(self) -> None:
        cpp = convert(COPYIN_F90)
        self.assertIn("fortran::byref(", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class ArgumentCopyinRunTests(unittest.TestCase):
    def test_temporary_is_passed_and_modified(self) -> None:
        # 5.0 copied in, + 1.0 = 6.0 (write-back is discarded by the caller)
        self.assertEqual(float(run(COPYIN_F90).strip()), 6.0)


if __name__ == "__main__":
    unittest.main()
