"""Symbol attributes are read from the resolved-symbol ``attrs`` flang
attaches to each ``Name``, rather than walked off the ``AttrSpec`` syntax.

Why it matters: Fortran lets attributes be applied via **standalone
statements** (``INTENT(IN) :: x`` after a separate ``REAL :: x``) as well
as inline on the declaration (``REAL, INTENT(IN) :: x``).  flang
consolidates both forms onto the symbol; the converter consumes the
consolidated set, so both produce identical translations.
"""

from __future__ import annotations

import unittest

from _support import convert, have_cxx, have_flang, run


# Same routine, two declaration styles — should produce the same C++
# signature.  We do a textual comparison of the generated signature line
# to assert equivalence.
INLINE_ATTRS_F90 = """\
subroutine sa(x, y, z)
  real, intent(in)    :: x
  real, optional      :: y
  real, intent(inout) :: z
  z = x + 1.0
  if (present(y)) z = z + y
end subroutine
"""

STANDALONE_ATTRS_F90 = """\
subroutine sa(x, y, z)
  real :: x, y, z
  intent(in)    :: x
  optional      :: y
  intent(inout) :: z
  z = x + 1.0
  if (present(y)) z = z + y
end subroutine
"""


def _signature_of(cpp: str, name: str) -> str:
    for line in cpp.splitlines():
        if f"void {name}(" in line:
            return line.strip().rstrip(" {")
    raise AssertionError(f"no signature for {name!r} in:\n{cpp}")


@unittest.skipUnless(have_flang(), "flang binary not available")
class StandaloneAttributeStatementTests(unittest.TestCase):
    def test_inline_and_standalone_attrs_produce_same_signature(self) -> None:
        inline = _signature_of(convert(INLINE_ATTRS_F90), "sa")
        standalone = _signature_of(convert(STANDALONE_ATTRS_F90), "sa")
        # Both forms must yield the identical C++ signature.
        self.assertEqual(inline, standalone)
        # And in both, x is const (intent in) and y is std::optional
        # (the standalone form was previously dropped by the converter).
        self.assertIn("const float& x", inline)
        self.assertIn("std::optional<float> y", inline)
        self.assertIn("float& z", inline)


# A scalar param that the body never writes locally normally gets demoted
# to ``const T&`` by the read-only inference.  But an explicit
# ``INTENT(INOUT)`` declaration is the programmer's contract — the
# converter must respect it and keep ``T&``, even if this body happens to
# only forward the value.
DECLARED_INOUT_F90 = """\
subroutine receiver(x)
  integer, intent(inout) :: x
  call passthrough(x)
end subroutine

subroutine passthrough(y)
  integer :: y
  ! y is read-only here, but `receiver`'s INTENT(INOUT) declares its `x`
  ! as inout regardless: the inference must not demote it.
end subroutine
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class DeclaredIntentRespectedTests(unittest.TestCase):
    def test_declared_inout_is_not_demoted(self) -> None:
        cpp = convert(DECLARED_INOUT_F90)
        # receiver's x stays std::int32_t&, not const std::int32_t&,
        # because INTENT(INOUT) was declared by the source.
        sig = _signature_of(cpp, "receiver")
        self.assertIn("std::int32_t& x", sig)
        self.assertNotIn("const std::int32_t& x", sig)


# An explicit ``real(8) function`` prefix and the body's actual result
# type should both produce the same C++ return type.  After the cleanup,
# both routes read the type off the function-defining Name's resolved
# symbol — so even when the prefix is unusual (RESULT clause, implicit
# typing, named-kind constant) the return type comes through unchanged.
PREFIX_RETURN_F90 = """\
real(kind=8) function dbl(x)
  real, intent(in) :: x
  dbl = x * 2.0d0
end function
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class FunctionReturnTypeFromSymbolTests(unittest.TestCase):
    def test_kind_8_return_type(self) -> None:
        cpp = convert(PREFIX_RETURN_F90)
        # The function returns double, not float — the (8) kind comes from
        # flang's resolved symbol, not parse-tree-spec walking.
        self.assertIn("double dbl(", cpp)


if __name__ == "__main__":
    unittest.main()
