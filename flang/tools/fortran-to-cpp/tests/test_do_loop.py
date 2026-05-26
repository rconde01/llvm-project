"""Counted DO loops — ``do i = lo, hi [, step]``.

A counted DO becomes a C++ ``for``.  A constant positive step uses a
plain ``i <= hi`` test; a step whose sign isn't known to be positive uses
a direction-aware test so the loop also runs when counting down.
"""

from __future__ import annotations

import unittest

from _support import convert, have_cxx, have_flang, run


DO_LOOP_F90 = """\
program p
  integer :: i, total
  total = 0
  do i = 1, 5
    total = total + i
  end do
  print *, total
  total = 0
  do i = 10, 2, -2
    total = total + i
  end do
  print *, total
end program
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class DoLoopEmitTests(unittest.TestCase):
    def test_unit_step_becomes_plain_for(self) -> None:
        cpp = convert(DO_LOOP_F90)
        self.assertIn("for (i = 1; i <= 5; ++i) {", cpp)

    def test_negative_step_is_direction_aware(self) -> None:
        cpp = convert(DO_LOOP_F90)
        self.assertIn("for (i = 10; (-2 >= 0 ? i <= 2 : i >= 2); i += -2) {", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class DoLoopRunTests(unittest.TestCase):
    def test_counts_up_and_down(self) -> None:
        nums = [int(t) for t in run(DO_LOOP_F90).split()]
        # 1+2+3+4+5 = 15 ; 10+8+6+4+2 = 30
        self.assertEqual(nums, [15, 30])


if __name__ == "__main__":
    unittest.main()
