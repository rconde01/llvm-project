"""Block IF — ``if (...) then / else if / else / end if``.

The block IF maps directly onto a C++ ``if / else if / else`` chain.
"""

from __future__ import annotations

import unittest

from _support import convert, have_cxx, have_flang, run


IF_F90 = """\
program p
  integer :: n
  do n = 1, 3
    if (n == 1) then
      print *, "one"
    else if (n == 2) then
      print *, "two"
    else
      print *, "other"
    end if
  end do
end program
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class IfThenElseEmitTests(unittest.TestCase):
    def test_maps_to_if_else_if_else(self) -> None:
        cpp = convert(IF_F90)
        self.assertIn("if (n == 1) {", cpp)
        self.assertIn("else if (n == 2) {", cpp)
        self.assertIn("else {", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class IfThenElseRunTests(unittest.TestCase):
    def test_each_branch_taken(self) -> None:
        lines = [l.strip() for l in run(IF_F90).splitlines() if l.strip()]
        self.assertEqual(lines, ["one", "two", "other"])


if __name__ == "__main__":
    unittest.main()
