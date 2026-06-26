"""Const-inference safety around dummy-procedure calls.

A routine that takes a procedure-typed dummy and calls it on one of its
own scalar params has an opaque write surface: at C++ template
instantiation time any actual procedure (including one we don't have
the source of) could write the arg.  ``_infer_readonly_scalar_params``
must therefore treat such a call as a local write so the dummy isn't
demoted to ``const T&`` (which would refuse to bind a non-const
``T&``-expecting actual through the generic lambda).
"""

from __future__ import annotations

import unittest

from _support import convert, have_cxx, have_flang, run


# ``cb`` is a dummy procedure; ``x`` is a scalar arg that the local body
# never assigns directly, but ``cb(x)`` could write through.  The body
# also doesn't read x, only forwards it.  Without the procedure-dummy
# fix, x would infer to intent(in) / ``const float&`` -- and a real
# writer for cb (``negate`` below) would force a const-vs-non-const
# binding mismatch in C++.
PROC_DUMMY_F90 = """\
subroutine forward(cb, x)
  external cb
  real :: x
  call cb(x)
end subroutine

subroutine negate(y)
  real :: y
  y = -y
end subroutine

program p
  real :: r
  external negate
  r = 5.0
  call forward(negate, r)
  print *, r
end program
"""


# Control: a routine whose callback is only ever called on *literal*
# args has no scalar param that needs preserving.  Tests that the
# inference doesn't over-conservatize unrelated params.
PROC_DUMMY_CONST_CONTROL_F90 = """\
subroutine forward2(cb, x)
  external cb
  real :: x      ! never written, never passed to cb -> may be const
  real :: tmp
  tmp = x + 1.0
  call cb(tmp)
end subroutine

subroutine show(y)
  real :: y
  print *, y
end subroutine

program p
  external show
  call forward2(show, 42.0)
end program
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class ProcDummyInferenceTests(unittest.TestCase):
    def test_scalar_param_passed_to_proc_dummy_stays_mutable(self) -> None:
        # x flows into cb(x) -- opaque writer -> x must stay ``float& x``,
        # never ``const float&``.
        cpp = convert(PROC_DUMMY_F90)
        # Find the forward routine's signature.
        for line in cpp.splitlines():
            if "void forward(" in line:
                self.assertIn("float& x", line)
                self.assertNotIn("const float& x", line)
                break
        else:
            self.fail("forward() prototype not found in:\n" + cpp)

    def test_unrelated_scalar_param_still_inferred_const(self) -> None:
        # forward2's x is never written and never passed to cb -> the
        # dummy-procedure fix shouldn't drag it down to mutable.
        cpp = convert(PROC_DUMMY_CONST_CONTROL_F90)
        for line in cpp.splitlines():
            if "void forward2(" in line:
                self.assertIn("const float& x", line)
                break
        else:
            self.fail("forward2() prototype not found in:\n" + cpp)


@unittest.skipUnless(have_flang() and have_cxx(),
                     "need flang and a C++20 compiler")
class ProcDummyInferenceRunTests(unittest.TestCase):
    def test_passing_writer_through_proc_dummy_writes_caller_var(self) -> None:
        # If the inference correctly leaves x mutable, the binding
        # compiles and ``r`` is negated to -5.
        out = run(PROC_DUMMY_F90).strip()
        self.assertEqual(float(out), -5.0)


if __name__ == "__main__":
    unittest.main()
