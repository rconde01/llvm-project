"""ENTRY — alternate entry points into a subprogram.

A subroutine (or function) may declare extra ``ENTRY`` points that share
its storage; calling one enters the routine at that statement and runs to
the end.  The converter turns each ENTRY into its own standalone function
whose body is the tail of the unit from that point on.
"""

from __future__ import annotations

import unittest

from _support import convert, have_cxx, have_flang, run


# ``accumulate`` and its ENTRY ``add_ten`` share the dummy ``x``.
#   call accumulate(a): a = a + 1, then falls through to + 10  -> +11
#   call add_ten(b):    enters past the first add               -> +10
ENTRY_F90 = """\
subroutine accumulate(x)
  real :: x
  x = x + 1.0
  entry add_ten(x)
  x = x + 10.0
end subroutine

program p
  real :: a, b
  a = 0.0
  call accumulate(a)
  b = 0.0
  call add_ten(b)
  print *, a, b
end program
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class EntryEmitTests(unittest.TestCase):
    def test_each_entry_becomes_a_function(self) -> None:
        cpp = convert(ENTRY_F90)
        # The primary routine and the ENTRY both become real functions.
        self.assertIn("void accumulate(", cpp)
        self.assertIn("void add_ten(", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class EntryRunTests(unittest.TestCase):
    def test_entry_shares_body_tail(self) -> None:
        nums = [float(t) for t in run(ENTRY_F90).split()]
        # accumulate(a): 0 -> +1 -> +10 = 11 ; add_ten(b): 0 -> +10 = 10
        self.assertEqual(nums, [11.0, 10.0])


if __name__ == "__main__":
    unittest.main()
