"""Storage association across mismatched types under an implicit interface.

FORTRAN 77 has no interface checking, so a routine may pass an actual of
one type to a dummy of another; the dummy then *aliases the actual's
storage* rather than receiving a converted value (e.g. MOVED copies a
``DOUBLE PRECISION`` array by calling the integer-copy routine MOVEI).

A C++ reference/view can't bind a different type, so where the binding
would otherwise fail the converter reinterprets the storage — matching the
by-reference aliasing.  Cases that already convert (a ``const``-ref or
by-value dummy) are left untouched, so no compiling call site changes.
"""

from __future__ import annotations

import unittest

from _support import convert_project, have_cxx, have_flang, run_project


# A double-precision array copied through an integer-copy routine: the
# integer dummy aliases the double storage (twice as many elements).
ARRAY_PUN_F77 = """\
      subroutine copyints(a, n, b)
      integer a(*), b(*), n, i
      do i = 1, n
         b(i) = a(i)
      end do
      end

      subroutine copydbls(a, n, b)
      double precision a(*), b(*)
      integer n
      call copyints(a, 2*n, b)
      end

      program p
      double precision src(2), dst(2)
      src(1) = 1.5d0
      src(2) = -2.5d0
      dst(1) = 0.0d0
      dst(2) = 0.0d0
      call copydbls(src, 2, dst)
      print *, dst(1), dst(2)
      end
"""


# A double-precision actual passed to an integer (intent inout) dummy: the
# dummy aliases the actual's storage.
SCALAR_PUN_F77 = """\
      subroutine roundtrip(code, out)
      integer code, out
      code = 777
      out = code
      end

      program p
      double precision x
      integer r
      call roundtrip(x, r)
      print *, r
      end
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class StoragePunEmitTests(unittest.TestCase):
    def test_array_actual_is_reinterpreted(self) -> None:
        cpp = convert_project(ARRAY_PUN_F77, suffix=".f")
        self.assertIn("ftn::reinterpret_array<int32_t>(a)", cpp)

    def test_scalar_actual_is_reinterpreted(self) -> None:
        cpp = convert_project(SCALAR_PUN_F77, suffix=".f")
        self.assertIn("ftn::storage_ref<int32_t>(x)", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class StoragePunRunTests(unittest.TestCase):
    def test_array_bytes_round_trip(self) -> None:
        # Copying the bytes of the double array reproduces its values.
        vals = run_project(ARRAY_PUN_F77, suffix=".f").split()
        self.assertEqual(float(vals[0]), 1.5)
        self.assertEqual(float(vals[1]), -2.5)

    def test_scalar_bytes_round_trip(self) -> None:
        # The integer written through the aliased storage reads back intact.
        self.assertEqual(int(run_project(SCALAR_PUN_F77, suffix=".f").strip()), 777)


if __name__ == "__main__":
    unittest.main()
