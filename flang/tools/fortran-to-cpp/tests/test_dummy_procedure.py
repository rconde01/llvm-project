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
    def test_dummy_is_a_template_param(self) -> None:
        # A procedure-taking routine is a function template; the dummy is a
        # deduced type, not a fixed std::function — so the compiler infers
        # the callback type from whatever is passed.
        cpp = convert(DUMMY_PROC_F90)
        self.assertIn("template <class F0>", cpp)
        self.assertIn("const F0& f", cpp)

    def test_actual_is_wrapped_in_a_generic_lambda(self) -> None:
        cpp = convert(DUMMY_PROC_F90)
        # square is passed as a generic lambda forwarding to it.
        self.assertIn("apply_twice([&](auto&&... _a)", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class DummyProcedureRunTests(unittest.TestCase):
    def test_calls_the_passed_function(self) -> None:
        # square(3) + square(3) = 9 + 9 = 18
        self.assertEqual(float(run(DUMMY_PROC_F90).strip()), 18.0)


# A dummy procedure that the receiving routine only *forwards* (never calls
# locally) gives no local clue to its signature.  Because the routine is a
# template, no inference is needed: ``driver`` and ``engine`` are templates
# on the callback type, and the deduced type flows through the forward to
# wherever it is finally called.  The third argument is a ``double`` output,
# which the generic-lambda wrapper writes through correctly.
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
    def test_forwarder_is_a_template(self) -> None:
        cpp = convert_project(FORWARDED_PROC_F90)
        # Both the caller-of and the forwarder-of the callback are templates
        # on its type; no concrete std::function signature is committed to.
        self.assertIn("template <class F0>\nvoid engine(", cpp)
        self.assertIn("template <class F0>\nvoid driver(", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class ForwardedDummyProcedureRunTests(unittest.TestCase):
    def test_output_argument_writes_through(self) -> None:
        # average(2, 6) writes m = 4 through the double& output parameter.
        self.assertEqual(float(run_project(FORWARDED_PROC_F90).strip()), 4.0)


# A *nested* dummy procedure — a callback whose own first argument is itself
# a callback (the SPICE geometry-finder ``UDFUNB(UDFUNS, et, bool)`` shape).
# ``solver`` calls ``ufb`` locally, so it learns ufb's nested type; ``driver``
# only *forwards* ufb, so its signature must be carried back from solver's
# slot — across the forwarding chain, and including the nested callback's own
# refined argument types — for the forwarded call to type-check.  The outer
# fixpoint re-runs inference until those refinements propagate.
NESTED_PROC_F77 = """\
      subroutine scalarf(et, val)
      double precision et, val
      val = et * 2.0d0
      end

      subroutine boolf(uf, et, bool)
      external uf
      double precision et, v
      logical bool
      call uf(et, v)
      bool = v .gt. 0.0d0
      end

      subroutine solver(ufs, ufb, et, found)
      external ufs, ufb
      double precision et
      logical found, b
      call ufb(ufs, et, b)
      found = b
      end

      subroutine driver(ufs, ufb, et, found)
      external ufs, ufb
      double precision et
      logical found
      call solver(ufs, ufb, et, found)
      end

      program p
      external scalarf, boolf
      double precision et
      logical found
      et = 3.0d0
      call driver(scalarf, boolf, et, found)
      print *, found
      end
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class NestedDummyProcedureEmitTests(unittest.TestCase):
    def test_nested_callbacks_are_templates(self) -> None:
        cpp = convert_project(NESTED_PROC_F77, suffix=".f")
        # ``boolf`` takes a callback (its own ``uf``); ``solver``/``driver``
        # take both a scalar callback and a procedure-of-procedure (``ufb``).
        # All are templates on their callback types — the nested case needs
        # no signature inference at all, because the compiler deduces the
        # whole nested type from whatever is passed.
        self.assertIn("template <class F0>\nvoid boolf(", cpp)
        self.assertIn("template <class F0, class F1>\nvoid solver(", cpp)
        self.assertIn("template <class F0, class F1>\nvoid driver(", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class NestedDummyProcedureRunTests(unittest.TestCase):
    def test_nested_callback_runs(self) -> None:
        # scalarf(3) = 6 > 0, so found is true.
        self.assertIn(run_project(NESTED_PROC_F77, suffix=".f").strip().upper()[:1], ("T", "1"))


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
        # ``second`` takes ``cmp`` as a (template) callback parameter ...
        self.assertIn("template <class F0>\nvoid second(const F0& cmp", cpp)
        # ... while ``first`` (which doesn't) declares it as an empty local
        # ``std::function`` for the RETURN-guarded dead call carried over
        # from ``second`` — a local can't be a template parameter, so it
        # keeps a concrete (never-called) type.
        self.assertIn("std::function<bool(float)> cmp{};", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class EntrySharedProcedureDummyCompileTests(unittest.TestCase):
    def test_compiles(self) -> None:
        # The dead ``cmp(c)`` in ``first`` must still type-check.
        compile_only(ENTRY_SHARED_PROC_F77, suffix=".f")


if __name__ == "__main__":
    unittest.main()
