"""Fortran type → C++ type translation.

Pure functions: given an AST sub-tree describing a Fortran type, return
the corresponding ``IRType``.  Kept separate from ``lowering`` so the
type rules are easy to audit and extend.

Translation rules (R4, R5, D3, D7):

  ============================== =================================
  Fortran                        C++
  ============================== =================================
  ``integer``                    ``std::int32_t``
  ``integer(kind=2)``            ``std::int16_t``
  ``integer(kind=4)``            ``std::int32_t``
  ``integer(kind=8)``            ``std::int64_t``
  ``real``                       ``float``
  ``real(kind=4)``               ``float``
  ``real(kind=8)``               ``double``
  ``double precision``           ``double``
  ``logical``                    ``bool``
  ``logical(kind=N)``            ``bool``
  ``character``                  ``fortran::FortranString<1>``
  ``character(len=N)``           ``fortran::FortranString<N>``
  ``character(len=*)``           ``std::string_view``    (intent(in))
  ``complex``                    ``std::complex<float>``
  ``complex(kind=8)``            ``std::complex<double>``
  ============================== =================================

Array types wrap any of the above in ``fortran::Array<T, Rank>`` (the
rank is determined by the ``ArraySpec`` node in the AST).
"""

from __future__ import annotations

from flang_ast import Node

from .ir import IRType


# Default kinds when no kind specifier is given.  These match flang's
# defaults for the targets we care about; they can be overridden via
# the ``-fdefault-integer-N`` / ``-fdefault-real-N`` family of flags
# but we don't honor those yet.
_DEFAULT_INTEGER_KIND = 4
_DEFAULT_REAL_KIND = 4
_DEFAULT_LOGICAL_KIND = 4


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def lower_type_spec(decl_type_spec: Node) -> IRType:
    """Lower a ``DeclarationTypeSpec`` node into an IRType.

    A ``DeclarationTypeSpec`` always has exactly one child describing
    the actual type — usually an ``IntrinsicTypeSpec`` for the cases
    we currently support.  Derived types are still TODO.
    """
    spec = decl_type_spec.first_child(
        "IntrinsicTypeSpec",
        "DeclarationTypeSpec::Type",
        "DeclarationTypeSpec::Class",
    )
    if spec is None:
        # Walk into the children to find an intrinsic spec wrapped in
        # variant nodes we don't model.
        for child in decl_type_spec.walk():
            if child.kind == "IntrinsicTypeSpec":
                spec = child
                break
    if spec is None:
        return _unknown_type(decl_type_spec)
    if spec.kind == "IntrinsicTypeSpec":
        return _lower_intrinsic(spec)
    return _unknown_type(spec)


# ---------------------------------------------------------------------------
# Intrinsic type lowering
# ---------------------------------------------------------------------------


def _lower_intrinsic(intrinsic_spec: Node) -> IRType:
    """Lower one of the children of ``IntrinsicTypeSpec``.

    The AST shape is ``IntrinsicTypeSpec`` → ``IntegerTypeSpec`` /
    ``IntrinsicTypeSpec::Real`` / etc.  We look at the kind of the
    inner child to dispatch.
    """
    # First non-transparent child is the actual type discriminator.
    # The dump-parse-tree NODE(IntrinsicTypeSpec, Real) macro yields
    # just ``"Real"`` (not ``"IntrinsicTypeSpec::Real"``).
    for child in intrinsic_spec.children:
        match child.kind:
            case "IntegerTypeSpec":
                return _make_integer(_extract_kind(child))
            case "Real":
                return _make_real(_extract_kind(child))
            case "DoublePrecision":
                return _make_real(8)
            case "Logical":
                return _make_logical(_extract_kind(child))
            case "Character":
                return _make_character(child)
            case "Complex":
                return _make_complex(_extract_kind(child))
            case "DoubleComplex":
                return _make_complex(8)
    return _unknown_type(intrinsic_spec)


def _make_integer(kind: int | None) -> IRType:
    k = kind if kind is not None else _DEFAULT_INTEGER_KIND
    cpp = {1: "std::int8_t", 2: "std::int16_t",
           4: "std::int32_t", 8: "std::int64_t"}.get(k, "std::int32_t")
    return IRType(cpp=cpp, fortran=f"integer(kind={k})", is_integer=True)


def _make_real(kind: int | None) -> IRType:
    k = kind if kind is not None else _DEFAULT_REAL_KIND
    cpp = {4: "float", 8: "double"}.get(k, "float")
    return IRType(cpp=cpp, fortran=f"real(kind={k})", is_real=True)


def _make_logical(kind: int | None) -> IRType:
    k = kind if kind is not None else _DEFAULT_LOGICAL_KIND
    # All Fortran logical kinds map to C++ bool; we ignore the byte
    # width since C++ has no fixed-width bool.
    return IRType(cpp="bool", fortran=f"logical(kind={k})", is_logical=True)


def _make_complex(kind: int | None) -> IRType:
    k = kind if kind is not None else _DEFAULT_REAL_KIND
    inner = "float" if k == 4 else "double"
    return IRType(cpp=f"std::complex<{inner}>", fortran=f"complex(kind={k})")


def _make_character(char_spec: Node) -> IRType:
    """Lower a ``CHARACTER`` declaration.

    ``CHARACTER(LEN=N)`` becomes ``fortran::FortranString<N>``;
    ``CHARACTER`` alone is ``FortranString<1>`` (Fortran's default).
    ``CHARACTER(LEN=*)`` (assumed length) becomes ``std::string_view``
    — only valid as an ``intent(in)`` parameter, which the lowering
    pass will enforce when it gets to parameters.
    """
    length = _extract_character_length(char_spec)
    if length == "*":
        return IRType(
            cpp="std::string_view",
            fortran="character(len=*)",
            is_character=True,
        )
    n = length if length is not None else 1
    return IRType(
        cpp=f"fortran::FortranString<{n}>",
        fortran=f"character(len={n})",
        is_character=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_kind(type_node: Node) -> int | None:
    """Try to pull an integer kind out of a type specifier.

    The AST has ``KindSelector`` → ... → ``IntLiteralConstant``.  We
    look for the first integer literal under the spec; if there is
    none, we return ``None`` to let the caller fall back on a default.
    """
    for n in type_node.walk():
        if n.kind == "IntLiteralConstant" and n.fortran:
            try:
                return int(n.fortran.split("_")[0])
            except ValueError:
                return None
    return None


def _extract_character_length(char_spec: Node) -> int | str | None:
    """Pull the length from a ``CHARACTER`` declaration.

    Returns the integer length, the literal string ``"*"`` for
    assumed length, or ``None`` if no length is specified.
    """
    # An asterisk shows up as a ``Star`` node.
    for n in char_spec.walk():
        if n.kind == "Star":
            return "*"
        if n.kind == "TypeParamValue":
            # TypeParamValue wraps the length expression.
            for sub in n.walk():
                if sub.kind == "Star":
                    return "*"
                if sub.kind == "IntLiteralConstant" and sub.fortran:
                    try:
                        return int(sub.fortran.split("_")[0])
                    except ValueError:
                        pass
    return None


def _unknown_type(node: Node) -> IRType:
    """Fallback when we don't recognize a type — keeps lowering going
    so the user still gets a (probably broken) translation they can
    inspect, rather than a hard error."""
    src = node.source.text if node.source else node.kind
    return IRType(cpp=f"/* TODO: unknown type {src!r} */ auto",
                  fortran=src)
