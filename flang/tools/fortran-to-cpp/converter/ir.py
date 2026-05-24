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

    array_static: bool = False
    """True when every array bound is a compile-time constant, so the
    array can be hoisted into a workspace struct sized at construction
    (vs an automatic array whose size depends on runtime arguments)."""

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
class IRMember:
    """Derived-type component access: ``base%field`` -> ``base.field``."""

    base: "IRExpr"
    field: str


@dataclass(frozen=True, slots=True)
class IRTriplet:
    """One ``lo:hi:stride`` subscript of an array section.

    Any of the three may be ``None`` (``a(:)`` -> all None; defaults
    are the array's lbound / ubound / 1)."""

    lower: "IRExpr | None" = None
    upper: "IRExpr | None" = None
    stride: "IRExpr | None" = None


@dataclass(frozen=True, slots=True)
class IRSection:
    """An array section ``a(sub, sub, ...)``.

    Each subscript is either a scalar ``IRExpr`` (a fixed index that
    drops that dimension) or an ``IRTriplet`` (a ranged dimension).
    The section's rank is the number of triplet subscripts.
    """

    array: str
    subscripts: tuple  # tuple[IRExpr | IRTriplet, ...]


@dataclass(frozen=True, slots=True)
class IRArrayConstructor:
    """``[e1, e2, ...]`` / ``(/ ... /)`` — a rank-1 array literal."""

    elements: tuple  # tuple[IRExpr, ...]


@dataclass(frozen=True, slots=True)
class IRImpliedDo:
    """An implied-do ``(items..., var=lo,hi[,step])``.

    Appears in array constructors (``[(i*i, i=1,5)]``) and in I/O item
    lists (``print *, (a(i), i=1,n)``).  Expanded into an explicit loop
    by the array-expansion pass (constructor form) or the emitter
    (I/O form)."""

    var: str
    lower: "IRExpr"
    upper: "IRExpr"
    step: "IRExpr | None"
    items: tuple  # tuple[IRExpr, ...]


@dataclass(frozen=True, slots=True)
class IRCast:
    """A type conversion: ``static_cast<cpp_type>(operand)``.

    Produced by the kind-dependent conversion intrinsics (INT, REAL,
    DBLE, ...) where the result type depends on a kind argument.
    """

    cpp_type: str
    operand: "IRExpr"


@dataclass(frozen=True, slots=True)
class IRRaw:
    """Escape hatch: emit ``text`` verbatim into the output.

    Used when we encounter an expression form we don't yet model but
    can salvage by using the original Fortran source (with a TODO
    marker added by the emitter).
    """

    text: str


IRExpr = Union[
    IRLiteral, IRName, IRBinaryOp, IRUnaryOp, IRFunctionCall, IRMember,
    IRCast, IRSection, IRArrayConstructor, IRImpliedDo, IRRaw
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
    """``print …``  /  ``write(unit, …) …`` output statement.

    When ``format`` is ``None`` the output is list-directed and the
    emitter renders a plain ``<<`` chain.  When ``format`` holds a
    Fortran format string (e.g. ``"(I5, 1X, F8.2)"``) the emitter maps
    each edit descriptor to inline ``std::format`` or a ``fortran::io``
    helper (see format.py / decision D5).
    """

    items: list[IRExpr] = field(default_factory=list)
    stream: str = "std::cout"  # C++ stream expression
    format: str | None = None
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRRead:
    """``read *, …`` / ``read(unit, …) …`` — list-directed input.

    Lowers to a ``>>`` chain on the input stream.  Each item is an
    lvalue expression (a variable, array element, or component).
    """

    items: list[IRExpr] = field(default_factory=list)
    stream: str = "std::cin"
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRStop:
    """``stop`` / ``stop <code>`` / ``stop "msg"`` / ``error stop``.

    Terminates the program (``std::exit``).  ``message`` is printed to
    stderr first when present; ``code`` is the integer exit status.
    """

    code: IRExpr | None = None
    message: str | None = None
    is_error: bool = False
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
    declare: bool = False
    """When True the emitter declares the index variable in the for-init
    (``for (fortran::index_t i = ...)``).  Set for compiler-synthesized
    loops (e.g. whole-array assignment expansion); user ``do`` loops use
    a pre-declared variable."""

    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRWhile:
    """``do while (cond) ; … ; end do``."""

    condition: IRExpr
    body: list["IRStatement"] = field(default_factory=list)
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRCaseClause:
    """One ``case (...)`` arm of a select-case construct."""

    values: list[IRExpr] = field(default_factory=list)
    """Single match values: ``case (1, 2, 3)`` -> three values."""

    ranges: list[tuple[IRExpr | None, IRExpr | None]] = field(
        default_factory=list
    )
    """Inclusive ranges: ``case (1:5)`` -> (1, 5).  An open end is
    ``None`` (``case (:0)`` -> (None, 0))."""

    body: list["IRStatement"] = field(default_factory=list)


@dataclass(slots=True)
class IRSelectCase:
    """``select case (expr) ; case ... ; case default ; end select``."""

    selector: IRExpr
    clauses: list[IRCaseClause] = field(default_factory=list)
    default_body: list["IRStatement"] | None = None
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRCycle:
    """``cycle`` — C++ ``continue;``."""

    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRExit:
    """``exit`` — C++ ``break;``."""

    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRAllocate:
    """``allocate(a(n))`` — re-sizes a heap-backed array.

    Lowers to a move-assignment of a freshly-sized ``fortran::Array``.
    ``cpp_type`` (the full ``fortran::Array<T, R>`` spelling) is filled
    in by a resolution pass once the declared type of ``obj`` is known.
    """

    obj: str
    extents: list[IRExpr] = field(default_factory=list)
    lowers: list[IRExpr] = field(default_factory=list)
    cpp_type: str = ""
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRDeallocate:
    """``deallocate(a)`` — releases an allocatable array's storage."""

    obj: str
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRBlock:
    """A scoped block: ``associate`` (``auto&&`` bindings) or ``block``
    (local declarations), each followed by a body and a closing brace.
    """

    bindings: list[tuple[str, "IRExpr"]] = field(default_factory=list)
    """``auto&& name = expr;`` pairs (from ASSOCIATE)."""

    locals: list["IRLocal"] = field(default_factory=list)
    """Block-local declarations (from BLOCK)."""

    body: list["IRStatement"] = field(default_factory=list)
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRWhere:
    """``where (mask) ... elsewhere ... end where`` — masked array
    assignment.

    Transient: the array-expansion pass turns it into a masked element
    loop (an IRDo nest wrapping an IRIf), so it never reaches the
    emitter.  ``mask`` is an array-valued logical expression; the
    bodies are whole-array assignments.
    """

    mask: IRExpr
    where_body: list["IRStatement"] = field(default_factory=list)
    elsewhere_body: list["IRStatement"] | None = None
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
    IRRead,
    IRStop,
    IRAllocate,
    IRDeallocate,
    IRIf,
    IRDo,
    IRWhile,
    IRSelectCase,
    IRCycle,
    IRExit,
    IRReturn,
    IRBlock,
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

    is_optional: bool = False
    """True for ``OPTIONAL`` dummy arguments."""

    leading_comments: list[Comment] = field(default_factory=list)
    """Comments that appeared immediately above this declaration."""

    trailing_comments: list[Comment] = field(default_factory=list)
    """Inline comments on the same line as the declaration."""


@dataclass(slots=True)
class IRParameter:
    """A subprogram formal parameter."""

    name: str
    type: IRType
    intent: Literal["in", "out", "inout"] = "inout"

    optional: bool = False
    """True for OPTIONAL dummy args; emitted as ``std::optional<T>`` with
    a ``= std::nullopt`` default (intent(in) scalars)."""

    def cpp_param_decl(self) -> str:
        """C++ parameter declaration string.

        Scalars use a reference (``T&`` or ``const T&`` per intent).
        Arrays use ``fortran::ArrayRef`` by value — ArrayRef is a
        small, non-owning view, so the caller's owning ``Array``
        converts implicitly and the function body can take slices
        without making the caller's storage assumption explicit.
        """
        if self.optional and not self.type.is_array:
            # intent(in) optional scalar -> std::optional with a default,
            # so trailing optional args can be omitted at the call site.
            return f"std::optional<{self.type.cpp}> {self.name} = std::nullopt"
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
class IRStateStruct:
    """A struct that bundles per-subprogram persistent state.

    Used for ``SAVE`` locals (one struct per subprogram that has any)
    and, in the future, for common blocks and module variables.  The
    state plumbing pass moves the relevant IRLocal entries onto the
    struct, generates a parameter for each subprogram that touches the
    state, and rewrites the body to reference ``<param>.<name>``.
    """

    cpp_type: str
    """Generated C++ type name (e.g. ``"CounterSave"``)."""

    fields: list[IRLocal] = field(default_factory=list)
    """Variables that moved onto the struct, in declared order."""


@dataclass(slots=True)
class IRStateBinding:
    """A ``auto& <name> = <param>.<field>;`` reference binding."""

    name: str
    param: str
    field: str


@dataclass(slots=True)
class IRStateParam:
    """A subprogram parameter for one piece of plumbed state."""

    name: str
    """Parameter name in the generated C++ (e.g. ``"counter_save"``)."""

    struct_type: str
    """The C++ type spelling (e.g. ``"CounterSave"``)."""

    owned_by: str
    """Canonical lower-cased name of the subprogram that owns this
    struct's *definition* — i.e. whose SAVE locals it groups.  Used
    when forwarding state through a call chain."""


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

    save_struct: IRStateStruct | None = None
    """The save struct *owned* by this subprogram (None if it has no
    SAVE locals).  Always added as the first state parameter."""

    common_uses: list["IRCommonUse"] = field(default_factory=list)
    """Common blocks this subprogram declares / references, in source
    order.  Populated by lowering; consumed by the state plumbing
    pass which synthesizes one shared struct per block name."""

    workspace: IRStateStruct | None = None
    """Per-routine workspace holding hoisted fixed-size local arrays
    (allocated once, threaded like other state).  None when the
    routine has no hoistable arrays (or is recursive)."""

    state_bindings: list["IRStateBinding"] = field(default_factory=list)
    """``auto& name = param.field;`` bindings emitted at the top of the
    body so it can reference plumbed state (common / save / module /
    workspace) by its original name and stay clean."""

    parent_module: str | None = None
    """Canonical name of the module that hosts this subprogram (for
    module procedures), else None.  A module procedure implicitly
    accesses its host module's variables."""

    used_modules: list[str] = field(default_factory=list)
    """Module names this subprogram pulls in via ``use`` (canonical
    lower-case), in source order."""

    state_params: list[IRStateParam] = field(default_factory=list)
    """State parameters this subprogram receives from its callers
    (own save struct + transitively-required ones from callees).
    Plumbed in front of the user-visible parameters at emit time."""


@dataclass(slots=True)
class IRCommonUse:
    """One ``common /name/ a, b, c`` declaration in a subprogram."""

    block_name: str
    """Canonical lower-cased block name; empty string for blank common."""

    member_names: list[str] = field(default_factory=list)
    """Member variable names, in declared order (canonical lower-case)."""


@dataclass(slots=True)
class IRDerivedType:
    """A user-defined ``type ... end type`` mapped to a C++ struct."""

    cpp_type: str
    """Generated struct name (CamelCase of the Fortran type name)."""

    fortran_name: str
    """Original Fortran type name (lower-cased)."""

    fields: list[IRLocal] = field(default_factory=list)
    """Component declarations, in source order."""


@dataclass(slots=True)
class IRModule:
    """A Fortran ``module`` — its variables become a shared state struct.

    Module *procedures* are lowered into ``IRTranslationUnit.subprograms``
    like any other subprogram, with ``parent_module`` set.
    """

    cpp_type: str
    """Generated state-struct name (e.g. ``"ConfigModule"``)."""

    fortran_name: str
    """Original module name (lower-cased)."""

    variables: list[IRLocal] = field(default_factory=list)
    """Module-level variable declarations (with any initializers)."""


@dataclass(slots=True)
class IRTranslationUnit:
    """The top-level container — everything emitted into one .cpp file."""

    subprograms: list[IRSubprogram] = field(default_factory=list)
    """Subprograms in callee-first dependency order."""

    derived_types: list[IRDerivedType] = field(default_factory=list)
    """User-defined types, emitted as structs ahead of everything."""

    modules: list[IRModule] = field(default_factory=list)
    """Modules with variables; each becomes a shared state struct."""

    common_structs: list[IRStateStruct] = field(default_factory=list)
    """One shared struct per common-block name, synthesized by the
    state plumbing pass."""

    source_file: str | None = None
    """Original Fortran path, for the ``// generated from …`` header."""
