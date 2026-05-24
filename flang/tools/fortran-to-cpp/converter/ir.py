"""Intermediate representation for the Fortran-to-C++ translator.

The IR is intentionally compact: every node is a frozen dataclass with
typed fields, no inheritance hierarchies, no string-keyed dicts.  The
lowering pass (``lowering.py``) turns a flang parse tree into IR; the
emitter (``emit.py``) walks the IR to produce C++ source text.

A new IR node should be added here as soon as a new Fortran construct
is supported.  Keep the IR small and concrete — model what we know how
to emit, not the Fortran grammar in its entirety.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Union

from flang_ast import Comment, SourceRange

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IRType:
    """A Fortran type, already lowered to its C++ rendering."""

    cpp: str
    """How to spell this type in C++ (e.g. ``"std::int32_t"``)."""

    fortran: str
    """Human-readable Fortran spelling (e.g. ``"integer(kind=4)"``).

    Kept for diagnostics and emitted as a comment when the C++ type
    isn't self-explanatory.
    """

    is_array: bool = False
    is_character: bool = False
    is_logical: bool = False
    is_integer: bool = False
    is_real: bool = False

    array_rank: int = 0
    """For ``is_array`` types, the number of dimensions."""

    array_extent_exprs: tuple[str, ...] = ()
    """Per-dimension extent expressions, as rendered C++ text."""

    array_lower_bound_exprs: tuple[str, ...] = ()
    """Per-dimension lower-bound expressions when explicit, else empty
    (extent-only constructor is used and lower bounds default to 1)."""

    element_type_cpp: str = ""
    """For arrays, the element type spelling (e.g. ``"std::int32_t"``)."""


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IRLiteral:
    """A Fortran literal (numeric, character, or logical).

    ``cpp_text`` is the verbatim C++ form (e.g. ``"42"``, ``"1.5f"``,
    ``'"hello"sv'``).  ``cpp_type`` is the explicit type if R4 says we
    need one (numeric literals); ``None`` for everything else.
    """

    cpp_text: str
    cpp_type: str | None = None


@dataclass(frozen=True, slots=True)
class IRName:
    """A bare identifier reference (a variable, parameter, …)."""

    name: str  # canonical lower-cased name as it appears in C++
    fortran: str  # source spelling, for comments / diagnostics


@dataclass(frozen=True, slots=True)
class IRBinaryOp:
    op: str  # one of "+", "-", "*", "/", "<", "<=", ">", ">=", "==", "!=", "&&", "||"
    lhs: "IRExpr"
    rhs: "IRExpr"


@dataclass(frozen=True, slots=True)
class IRUnaryOp:
    op: str  # one of "-", "+", "!"
    operand: "IRExpr"


@dataclass(frozen=True, slots=True)
class IRFunctionCall:
    callee: str
    args: tuple["IRExpr", ...]


@dataclass(frozen=True, slots=True)
class IRRaw:
    """Escape hatch: emit ``text`` verbatim into the output.

    Used when we encounter an expression form we don't yet model but
    can salvage by using the original Fortran source (with a TODO
    marker added by the emitter).
    """

    text: str


IRExpr = Union[
    IRLiteral, IRName, IRBinaryOp, IRUnaryOp, IRFunctionCall, IRRaw
]


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IRComment:
    """A standalone comment block (always rendered as // lines)."""

    comments: list[Comment]


@dataclass(slots=True)
class IRAssignment:
    target: IRExpr
    value: IRExpr
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRCall:
    """A subroutine call (statement, not an expression)."""

    callee: str
    args: list[IRExpr] = field(default_factory=list)
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRPrint:
    """``print *, …``  /  ``write(unit, *) …`` — list-directed only for now.

    The emitter renders this as a chain of ``<<`` operators on the
    chosen stream (``std::cout`` for ``print``, the bound stream for
    ``write``).
    """

    items: list[IRExpr] = field(default_factory=list)
    stream: str = "std::cout"  # C++ stream expression
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRIf:
    """``if … then / else if / else / end if`` cascade."""

    branches: list[tuple[IRExpr, list["IRStatement"]]] = field(
        default_factory=list
    )
    else_body: list["IRStatement"] | None = None
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRDo:
    """``do i = lo, hi[, step] ; … ; end do`` — counted form only."""

    var: str
    lower: IRExpr
    upper: IRExpr
    step: IRExpr | None
    body: list["IRStatement"] = field(default_factory=list)
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRReturn:
    value: IRExpr | None = None
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRUnsupported:
    """A construct the lowering pass doesn't yet model.

    The emitter renders it as a ``// TODO:`` block carrying the
    original source text, so the generated C++ stays compilable
    (after the user fills in the gap).
    """

    kind: str
    source_text: str
    note: str = ""
    leading_comments: list[Comment] = field(default_factory=list)


IRStatement = Union[
    IRAssignment,
    IRCall,
    IRPrint,
    IRIf,
    IRDo,
    IRReturn,
    IRComment,
    IRUnsupported,
]


# ---------------------------------------------------------------------------
# Declarations & subprograms
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IRLocal:
    """A local variable declaration inside a subprogram."""

    name: str
    type: IRType
    initializer: IRExpr | None = None
    is_parameter: bool = False
    """True for ``PARAMETER`` constants — emit as ``constexpr``."""

    is_save: bool = False
    """True for ``SAVE``d locals — moved into the per-subprogram save struct."""

    intent: Literal["in", "out", "inout"] | None = None
    """Set when the declaration carried an ``INTENT(...)`` attribute.
    Used by the lowering pass to recognize formal parameters.
    """

    leading_comments: list[Comment] = field(default_factory=list)
    """Comments that appeared immediately above this declaration."""


@dataclass(slots=True)
class IRParameter:
    """A subprogram formal parameter."""

    name: str
    type: IRType
    intent: Literal["in", "out", "inout"] = "inout"

    def cpp_param_decl(self) -> str:
        """C++ parameter declaration string.

        Scalars use a reference (``T&`` or ``const T&`` per intent).
        Arrays use ``fortran::ArrayRef`` by value — ArrayRef is a
        small, non-owning view, so the caller's owning ``Array``
        converts implicitly and the function body can take slices
        without making the caller's storage assumption explicit.
        """
        if self.type.is_array:
            const_q = "const " if self.intent == "in" else ""
            return (
                f"fortran::ArrayRef<{const_q}{self.type.element_type_cpp}, "
                f"{self.type.array_rank}> {self.name}"
            )
        if self.intent == "in":
            return f"const {self.type.cpp}& {self.name}"
        return f"{self.type.cpp}& {self.name}"


@dataclass(slots=True)
class IRSubprogram:
    """A single subprogram (main / function / subroutine)."""

    name: str  # canonical lower-cased
    display_name: str  # original source spelling
    kind: Literal["main", "function", "subroutine"]
    parameters: list[IRParameter] = field(default_factory=list)
    return_type: IRType | None = None
    """Set for functions; ``None`` for main and subroutines."""

    locals: list[IRLocal] = field(default_factory=list)
    body: list[IRStatement] = field(default_factory=list)
    leading_comments: list[Comment] = field(default_factory=list)
    source: SourceRange | None = None


@dataclass(slots=True)
class IRTranslationUnit:
    """The top-level container — everything emitted into one .cpp file."""

    subprograms: list[IRSubprogram] = field(default_factory=list)
    """Subprograms in callee-first dependency order."""

    source_file: str | None = None
    """Original Fortran path, for the ``// generated from …`` header."""
