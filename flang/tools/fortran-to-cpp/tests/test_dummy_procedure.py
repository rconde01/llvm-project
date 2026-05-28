"""Dummy procedures — a function passed as an argument and called inside.

``EXTERNAL f`` marks ``f`` a procedure dummy; the converter gives it a
``std::function`` parameter and, at the call site, wraps the actual
routine in a lambda (so any threaded state is captured).
"""

from __future__ import annotations

import unittest

from _support import (
    compile_only,
    convert,
    convert_project,
    have_cxx,
    have_flang,
    run,
    run_project,
)


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


# A dummy procedure that the receiving routine only *forwards* (never calls
# locally) gives the per-routine pass no argument count or types to work
# from.  The whole-program signature inference recovers them from the actual
# routine passed in — here a 3-argument callback whose third argument is a
# ``double`` *output*, which a float-based wrapper could not bind.
FORWARDED_PROC_F90 = """\
      subroutine engine(refn, lo, hi, mid)
      external refn
      double precision lo, hi, mid
      mid = 0.0d0
      call refn(lo, hi, mid)
      end

      subroutine driver(refn, lo, hi, mid)
      external refn
      double precision lo, hi, mid
      call engine(refn, lo, hi, mid)
      end

      subroutine average(a, b, m)
      double precision a, b, m
      m = (a + b) / 2.0d0
      end

      program p
      external average
      double precision m
      call driver(average, 2.0d0, 6.0d0, m)
      print *, m
      end
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class ForwardedDummyProcedureEmitTests(unittest.TestCase):
    def test_signature_inferred_from_actual(self) -> None:
        cpp = convert_project(FORWARDED_PROC_F90)
        # The forwarder's callback type carries the actual's real types,
        # not float — including the double& output parameter.
        self.assertIn("std::function<void(const double&, const double&, double&)>", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class ForwardedDummyProcedureRunTests(unittest.TestCase):
    def test_output_argument_writes_through(self) -> None:
        # average(2, 6) writes m = 4 through the double& output parameter.
        self.assertEqual(float(run_project(FORWARDED_PROC_F90).strip()), 4.0)


# A dummy procedure of one ENTRY is referenced by code that, in another
# entry's standalone body, falls *after* a RETURN (Fortran entries share a
# single linear body).  That sibling entry doesn't receive the procedure as
# an argument, so the dead call would name an undeclared symbol.  The
# converter declares an empty ``std::function`` local in each such entry so
# the unreachable call still type-checks.
ENTRY_SHARED_PROC_F77 = """\
      subroutine multi(a, out)
      real a, out, b, c
      logical cmp
      external cmp
      out = a
      return

      entry first(b, out)
      out = b + 1.0
      return

      entry second(cmp, c, out)
      if (cmp(c)) then
         out = -c
      end if
      return
      end
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class EntrySharedProcedureDummyTests(unittest.TestCase):
    def test_sibling_entry_declares_proc_local(self) -> None:
        cpp = convert(ENTRY_SHARED_PROC_F77, suffix=".f")
        # ``second`` takes ``cmp`` as a parameter ...
        self.assertIn(
            "void second(const std::function<bool(float)>& cmp", cpp
        )
        # ... while ``first`` (which doesn't) declares it as an empty local
        # for the RETURN-guarded dead call carried over from ``second``.
        self.assertIn("std::function<bool(float)> cmp{};", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class EntrySharedProcedureDummyCompileTests(unittest.TestCase):
    def test_compiles(self) -> None:
        # The dead ``cmp(c)`` in ``first`` must still type-check.
        compile_only(ENTRY_SHARED_PROC_F77, suffix=".f")


if __name__ == "__main__":
    unittest.main()
