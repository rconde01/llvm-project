"""Character substrings — ``s(lo:hi)``.

A substring selects a 1-based inclusive range of a character variable.
The converter lowers it to the runtime string's ``operator()(lo, hi)``
slice; an omitted bound defaults (lower -> 1, upper -> the length).
"""

from __future__ import annotations

import unittest

from _support import convert, have_cxx, have_flang, run


SUBSTRING_F90 = """\
program p
  character(len=11) :: s
  s = "hello world"
  print *, s(1:5)
  print *, s(7:11)
  print *, s(7:)
end program
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class SubstringEmitTests(unittest.TestCase):
    def test_substring_lowers_to_two_arg_call(self) -> None:
        cpp = convert(SUBSTRING_F90)
        # s(lo:hi) -> s(lo, hi); an open upper bound uses the length.
        self.assertIn("s(1, 5)", cpp)
        self.assertIn("s(7, 11)", cpp)
        self.assertIn("s(7, fortran::len(s))", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class SubstringRunTests(unittest.TestCase):
    def test_substring_slices(self) -> None:
        lines = [l.strip() for l in run(SUBSTRING_F90).splitlines() if l.strip()]
        self.assertEqual(lines, ["hello", "world", "world"])


if __name__ == "__main__":
    unittest.main()
