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
  print *, s(:5)
end program
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class SubstringEmitTests(unittest.TestCase):
    def test_substring_lowers_to_two_arg_call(self) -> None:
        cpp = convert(SUBSTRING_F90)
        # s(lo:hi) -> s(lo, hi); an open upper bound uses the length.
        self.assertIn("s(1, 5)", cpp)
        self.assertIn("s(7, 11)", cpp)
        self.assertIn("s(7, ftn::len(s))", cpp)

    def test_open_lower_bound_defaults_to_one(self) -> None:
        # s(:5) is s(1:5); the omitted *lower* must default to 1 (it was
        # previously misread as s(5:) because the empty slot is dropped).
        cpp = convert(SUBSTRING_F90)
        self.assertIn("s(1, 5)", cpp)
        self.assertNotIn("s(5, ftn::len(s))", cpp)


SUBSTRING_CMP_F90 = """\
program p
  character(len=11) :: s
  s = "abcdeabcde "
  if (s(1:5) == s(6:10)) then
    print *, "eq"
  else
    print *, "ne"
  end if
  if (s(1:5) /= s(7:11)) then
    print *, "ne2"
  end if
end program
"""


# In-place substring-to-substring assignment: compact a string by copying
# each kept character down (``s(put:put) = s(i:i)``), the SPICE ZZTIME
# tokenizer's ZZREMT idiom.  A Substring RHS must copy *characters*, not the
# proxy's pointer -- otherwise the implicit copy-assignment made it a silent
# no-op and the compaction left the string unchanged.
SUBSTRING_SELFCOPY_F90 = """\
program p
  character(len=11) :: s
  integer :: i, put
  s = "a.b.c.d.e.f"
  put = 0
  do i = 1, 11
     if (s(i:i) /= ".") then
        put = put + 1
        s(put:put) = s(i:i)
     end if
  end do
  print *, s(1:put)
end program
"""


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class SubstringSelfCopyRunTests(unittest.TestCase):
    def test_in_place_compaction(self) -> None:
        # Removing the '.' separators compacts "a.b.c.d.e.f" to "abcdef".
        self.assertEqual(run(SUBSTRING_SELFCOPY_F90).strip(), "abcdef")


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class SubstringRunTests(unittest.TestCase):
    def test_substring_slices(self) -> None:
        lines = [l.strip() for l in run(SUBSTRING_F90).splitlines() if l.strip()]
        # s(1:5)="hello", s(7:11)="world", s(7:)="world", s(:5)="hello"
        self.assertEqual(lines, ["hello", "world", "world", "hello"])

    def test_substring_comparison(self) -> None:
        # s(1:5)=="abcde"==s(6:10) -> eq; s(7:11)=="bcde " differs -> ne2.
        lines = [l.strip() for l in run(SUBSTRING_CMP_F90).splitlines() if l.strip()]
        self.assertEqual(lines, ["eq", "ne2"])


if __name__ == "__main__":
    unittest.main()
