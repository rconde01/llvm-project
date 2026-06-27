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


# The "umbrella subroutine" idiom: one routine whose only purpose is to
# host a block of bare-``SAVE`` state shared across several ENTRY points
# (SPICE's T_STAT, KEEPER, ISON, ...).  ``setn`` stores, ``incr`` bumps,
# ``getn`` retrieves -- all three must see the *same* ``n``.
UMBRELLA_F77 = """\
      SUBROUTINE UMB ( X )
      INTEGER N
      DOUBLE PRECISION X
      SAVE
      N = NINT( X )
      RETURN
      ENTRY INCR ( )
      N = N + 1
      RETURN
      ENTRY GETN ( X )
      X = N
      RETURN
      END

      PROGRAM P
      DOUBLE PRECISION V
      CALL UMB ( 5.0D0 )
      CALL INCR
      CALL INCR
      CALL GETN ( V )
      PRINT *, V
      END
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class EntryEmitTests(unittest.TestCase):
    def test_each_entry_becomes_a_function(self) -> None:
        cpp = convert(ENTRY_F90)
        # The primary routine and the ENTRY both become real functions.
        self.assertIn("void accumulate(", cpp)
        self.assertIn("void add_ten(", cpp)

    def test_umbrella_entries_share_one_save_struct(self) -> None:
        cpp = convert(UMBRELLA_F77)
        # A single SAVE struct holds the shared ``n`` ...
        self.assertIn("struct UmbSave {", cpp)
        # ... threaded through the primary and *every* entry under one name,
        # each binding ``n`` to the same field (not a private local).
        for sig in ("void umb(UmbSave&", "void incr(UmbSave&",
                    "void getn(UmbSave&"):
            self.assertIn(sig, cpp)
        self.assertEqual(cpp.count("auto& n = umb_save.n;"), 3)
        # The dummy ``x`` stays a parameter -- never pulled into the struct.
        self.assertNotIn("umb_save.x", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class EntryRunTests(unittest.TestCase):
    def test_entry_shares_body_tail(self) -> None:
        nums = [float(t) for t in run(ENTRY_F90).split()]
        # accumulate(a): 0 -> +1 -> +10 = 11 ; add_ten(b): 0 -> +10 = 10
        self.assertEqual(nums, [11.0, 10.0])

    def test_umbrella_shared_save_state_persists(self) -> None:
        # umb(5)->n=5; incr;incr->n=7; getn->v=7.  A private-per-entry ``n``
        # would read 0; the shared SAVE struct yields 7.
        nums = [float(t) for t in run(UMBRELLA_F77).split()]
        self.assertEqual(nums, [7.0])

    def test_save_data_init_runs_once_not_per_entry(self) -> None:
        # ``DATA cnt /0/`` on a SAVE var is a load-time init; if it were
        # re-run in each entry's prologue (the SPICE TRCPKG/CHKOUT bug) the
        # counter would reset every call.  push;push;pop must see 2, and the
        # body must not contain a ``cnt = 0`` reset.
        src = (
            "      SUBROUTINE TP ( D )\n"
            "      INTEGER D, CNT\n"
            "      SAVE\n"
            "      DATA CNT /0/\n"
            "      RETURN\n"
            "      ENTRY PUSH ( )\n"
            "      CNT = CNT + 1\n"
            "      RETURN\n"
            "      ENTRY POP ( D )\n"
            "      D = CNT\n"
            "      RETURN\n"
            "      END\n"
            "      PROGRAM T\n"
            "      INTEGER V\n"
            "      CALL PUSH\n"
            "      CALL PUSH\n"
            "      CALL POP ( V )\n"
            "      PRINT *, V\n"
            "      END\n"
        )
        cpp = convert(src)
        # DATA init became the SAVE-struct field initializer, run once.
        self.assertIn("int32_t cnt = 0;", cpp)
        # ... and is no longer a per-call body assignment.
        self.assertNotIn("cnt = 0;", cpp.split("struct", 1)[-1].split("};", 1)[-1])
        self.assertEqual([float(t) for t in run(src).split()], [2.0])


if __name__ == "__main__":
    unittest.main()
