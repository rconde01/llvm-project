"""Tests for the compile-time-lower-bound array emit path."""

from __future__ import annotations

import unittest
from io import StringIO

from converter.emit import _emit_local
from converter.ir import IRLiteral, IRLocal, IRType
from converter.static_lower import (
    try_static_lower_literals as _try_static_lower_literals,
)


def _emit(loc: IRLocal) -> str:
    out = StringIO()
    _emit_local(out, loc, indent=0)
    return out.getvalue()


def _array_type(*, rank: int, lower_exprs: tuple[str, ...],
                extent_exprs: tuple[str, ...]) -> IRType:
    return IRType(
        cpp=f"ftn::Array<float, {rank}>",
        fortran="real",
        is_array=True,
        is_real=True,
        array_rank=rank,
        array_extent_exprs=extent_exprs,
        array_lower_bound_exprs=lower_exprs,
        element_type_cpp="float",
    )


class TryStaticLowerLiteralsTests(unittest.TestCase):
    def test_empty_input_returns_none(self) -> None:
        self.assertIsNone(_try_static_lower_literals(()))

    def test_single_positive_literal(self) -> None:
        self.assertEqual(_try_static_lower_literals(("0",)), (0,))

    def test_negative_literal(self) -> None:
        self.assertEqual(_try_static_lower_literals(("-5",)), (-5,))

    def test_signed_with_plus(self) -> None:
        self.assertEqual(_try_static_lower_literals(("+3",)), (3,))

    def test_paren_wrapped_signed(self) -> None:
        # The converter sometimes wraps signed literals in parens.
        self.assertEqual(_try_static_lower_literals(("(-2)",)), (-2,))

    def test_mixed_positive_and_negative(self) -> None:
        self.assertEqual(
            _try_static_lower_literals(("0", "-1", "1")),
            (0, -1, 1),
        )

    def test_non_literal_returns_none(self) -> None:
        # A name reference can't be a compile-time integer at emit time.
        self.assertIsNone(_try_static_lower_literals(("n",)))

    def test_mixed_literal_and_non_literal_returns_none(self) -> None:
        # Any non-literal disqualifies the whole tuple.
        self.assertIsNone(_try_static_lower_literals(("0", "n")))

    def test_literal_with_cpp_suffix(self) -> None:
        # ``3L`` is still an integer literal at this layer.
        self.assertEqual(_try_static_lower_literals(("3L",)), (3,))


class EmitLocalStaticLowerTests(unittest.TestCase):
    def test_default_lower_uses_runtime_form(self) -> None:
        # No declared lower bounds -> runtime form with extents only.
        loc = IRLocal(
            name="a",
            type=_array_type(rank=1, lower_exprs=(), extent_exprs=("10",)),
        )
        cpp = _emit(loc)
        self.assertIn("ftn::Array<float, 1> a{{10}};", cpp)
        # Sanity: no static NTTP form on default-lb arrays.
        self.assertNotIn("std::array<ftn::index_t,", cpp)

    def test_literal_lower_uses_static_form(self) -> None:
        # Explicit lb=0 -> compile-time-lower-bounds form: the bound is
        # spelled in the type and the constructor takes extents only.
        loc = IRLocal(
            name="a",
            type=_array_type(rank=1, lower_exprs=("0",), extent_exprs=("10",)),
        )
        cpp = _emit(loc)
        self.assertIn(
            "ftn::Array<float, 1, std::array<ftn::index_t, 1>{0}> a{{10}};",
            cpp,
        )

    def test_negative_literal_lower_uses_static_form(self) -> None:
        loc = IRLocal(
            name="b",
            type=_array_type(rank=1, lower_exprs=("-5",), extent_exprs=("11",)),
        )
        cpp = _emit(loc)
        self.assertIn(
            "ftn::Array<float, 1, std::array<ftn::index_t, 1>{-5}> b{{11}};",
            cpp,
        )

    def test_rank2_mixed_literal_lower_uses_static_form(self) -> None:
        loc = IRLocal(
            name="m",
            type=_array_type(
                rank=2, lower_exprs=("0", "-1"), extent_exprs=("4", "3")
            ),
        )
        cpp = _emit(loc)
        self.assertIn(
            "ftn::Array<float, 2, std::array<ftn::index_t, 2>{0,-1}> m{{4, 3}};",
            cpp,
        )

    def test_non_literal_lower_falls_back_to_runtime(self) -> None:
        # ``a(n:m)`` where n, m are subprogram parameters -> runtime form.
        loc = IRLocal(
            name="a",
            type=_array_type(rank=1, lower_exprs=("n",), extent_exprs=("m - n + 1",)),
        )
        cpp = _emit(loc)
        self.assertIn(
            "ftn::Array<float, 1> a{{n}, {m - n + 1}};",
            cpp,
        )
        self.assertNotIn("std::array<ftn::index_t,", cpp)

    def test_static_form_with_scalar_initializer(self) -> None:
        # ``real :: a(0:9) = 0.0`` -> static-lower form using the
        # (extents, fill) ctor that the runtime grew alongside the NTTP.
        loc = IRLocal(
            name="a",
            type=_array_type(rank=1, lower_exprs=("0",), extent_exprs=("10",)),
            initializer=IRLiteral("0.0f"),
        )
        cpp = _emit(loc)
        self.assertIn(
            "ftn::Array<float, 1, std::array<ftn::index_t, 1>{0}> a{{10}, 0.0f};",
            cpp,
        )

    def test_runtime_form_with_scalar_initializer(self) -> None:
        # ``real :: a(n) = 0.0`` -> runtime fill ctor (lower defaults to 1).
        loc = IRLocal(
            name="a",
            type=_array_type(rank=1, lower_exprs=(), extent_exprs=("n",)),
            initializer=IRLiteral("0.0f"),
        )
        cpp = _emit(loc)
        self.assertIn(
            "ftn::Array<float, 1> a{{1}, {n}, 0.0f};",
            cpp,
        )


class ParamDeclStaticLowerTests(unittest.TestCase):
    """``IRParameter.cpp_param_decl`` switches to the static-``Lower`` form
    when every declared lower bound on the dummy is a literal integer."""

    def _array_param(self, *, name: str, rank: int, lower_exprs,
                     extent_exprs, intent: str = "in"):
        from converter.ir import IRParameter
        return IRParameter(
            name=name,
            type=_array_type(rank=rank, lower_exprs=lower_exprs,
                             extent_exprs=extent_exprs),
            intent=intent,
        )

    def test_default_lb_uses_runtime_arrayref(self) -> None:
        p = self._array_param(
            name="a", rank=1, lower_exprs=(), extent_exprs=("10",),
            intent="inout",
        )
        decl = p.cpp_param_decl(with_default=False)
        self.assertEqual(decl, "ftn::ArrayRef<float, 1> a")

    def test_literal_lb_uses_static_arrayref(self) -> None:
        p = self._array_param(
            name="a", rank=1, lower_exprs=("0",), extent_exprs=("10",),
            intent="inout",
        )
        decl = p.cpp_param_decl(with_default=False)
        self.assertEqual(
            decl,
            "ftn::ArrayRef<float, 1, std::array<ftn::index_t, 1>{0}> a",
        )

    def test_negative_lb_uses_static_arrayref(self) -> None:
        p = self._array_param(
            name="b", rank=1, lower_exprs=("-3",), extent_exprs=("7",),
            intent="in",
        )
        decl = p.cpp_param_decl(with_default=False)
        self.assertEqual(
            decl,
            "ftn::ArrayRef<const float, 1, "
            "std::array<ftn::index_t, 1>{-3}> b",
        )

    def test_non_literal_lb_falls_back(self) -> None:
        # ``a(n:m)`` -- n, m are subprogram parameters -> runtime form.
        p = self._array_param(
            name="a", rank=1, lower_exprs=("n",), extent_exprs=("m - n + 1",),
            intent="inout",
        )
        decl = p.cpp_param_decl(with_default=False)
        self.assertEqual(decl, "ftn::ArrayRef<float, 1> a")


if __name__ == "__main__":
    unittest.main()
