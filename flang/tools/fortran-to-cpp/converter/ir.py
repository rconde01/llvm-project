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

    is_pointer: bool = False
    """True for POINTER variables: scalar pointers are ``T*``, array
    pointers are non-owning ``fortran::ArrayRef``."""

    element_type_cpp: str = ""
    """For arrays, the element type spelling (e.g. ``"std::int32_t"``)."""

    is_procedure: bool = False
    """True for a dummy-procedure parameter: a function/subroutine passed
    as an argument (``cpp`` is a ``std::function<...>`` spelling).  Call
    sites wrap the actual procedure in a state-capturing lambda."""

    proc_arity: int = 0
    """For ``is_procedure`` types, how many (scalar) arguments the dummy
    procedure is invoked with inside the routine."""


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

    arg_categories: tuple[str, ...] = ()
    """Resolved expression category for each positional argument, mirroring
    ``args`` (``"variable"`` / ``"constant"`` / ``"expression"``).  Set at
    lowering time from the analyzed Expr; empty when the call came from
    code paths that don't carry it (synthetic helpers, intrinsics)."""


@dataclass(frozen=True, slots=True)
class IRMember:
    """Derived-type component access: ``base%field`` -> ``base.field``."""

    base: "IRExpr"
    field: str


@dataclass(frozen=True, slots=True)
class IRSubstr:
    """A character substring ``base(lo:hi)`` -> ``base(lo, hi)`` (the
    runtime FortranString / CharRef 1-based inclusive slice).  ``base`` is
    any character designator — a name, an array element ``a(i)``, a
    component — so it composes (``a(i)(lo:hi)``)."""

    base: "IRExpr"
    lo: "IRExpr"
    hi: "IRExpr"


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


@dataclass(frozen=True, slots=True)
class IRLambda:
    """A statement function lowered to a generic C++ lambda:
    ``[&](auto p, ...) { return <body>; }``."""

    params: tuple[str, ...]
    body: "IRExpr"


IRExpr = Union[
    IRLiteral, IRName, IRBinaryOp, IRUnaryOp, IRFunctionCall, IRMember,
    IRCast, IRSection, IRArrayConstructor, IRImpliedDo, IRRaw, IRLambda
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
    arg_categories: tuple[str, ...] = ()
    """Resolved expression category for each positional argument, mirroring
    ``args``.  See :class:`IRFunctionCall.arg_categories`."""
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
    # When set, the FORMAT is built at run time (e.g. ``WRITE(s, FMTVAR)``
    # with FMTVAR assembled by REPMI).  The emitter routes items through
    # ``fortran::io::format_record(<format_expr>, items...)`` -- a runtime
    # format interpreter -- instead of inline ``std::format`` calls.
    # Mutually exclusive with ``format``.
    format_expr: "IRExpr | None" = None
    # When set, this is an *internal file* write: the formatted record is
    # built and assigned to this (character-variable) lvalue rather than
    # written to ``stream``.
    internal_unit: "IRExpr | None" = None
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
    # When set, this is an *internal file* read: items are parsed from
    # this (character-variable) lvalue rather than from ``stream``.
    internal_unit: "IRExpr | None" = None
    # ``unit_text``: the unit number (or ``*``/``"*"``) for a unit-directed
    # read.  Filled when emitting fixed-width formatted reads so we can
    # call ``_units.in(<unit>)`` rather than reach into ``stream`` text.
    unit_text: str | None = None
    # Fixed-width formatted read: pre-resolved field slices.  Each entry
    # is ``(target, kind, off, width, dec)`` where ``kind`` is ``"int"`` or
    # ``"real"``.  Set by lowering when a sequential ``READ(unit, fmt)``
    # has a compile-time format spec that maps to fixed-width columns; the
    # emitter reads one record (``getline``) and slices fields by offset
    # rather than the list-directed ``>>`` chain (which space-tokenizes
    # and ignores column widths, producing wrong values for files like
    # SPICE/IRI's ``apf107.dat``).
    fields: "list[list[tuple[IRExpr, str, int, int, int]]] | None" = None
    """A list of records; each record is a list of
    ``(target, kind, offset, width, decimals)`` tuples.  Multi-record
    formats (item count exceeds a single format cycle) read one record
    per cycle."""
    # Fortran ``END=label`` -- jump to label on end-of-file.  Captured by
    # lowering; expanded into a synthetic IRIf+IRGoto by
    # ``_expand_io_label_jumps`` before the structuring pass, so the
    # label resolves through the normal goto-structuring path.
    end_label: int | None = None
    # Fortran ``ERR=label`` -- jump to label on a (non-EOF) I/O error.
    err_label: int | None = None
    # Fortran ``IOSTAT=var`` -- target to receive the read's status: 0 on
    # success, -1 on EOF, positive on error.  Assigned after the read.
    iostat_target: "IRExpr | None" = None
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRDirectRead:
    """``read(unit, fmt, REC=n) items`` — record-based formatted read.

    A direct-access OPEN gives each record a fixed ``recl``-byte slot; the
    read fetches record ``rec`` and parses items at fixed offsets per the
    FORMAT spec.  ``fields`` is one ``(target, kind, offset, width,
    decimals)`` tuple per scalar item, where ``kind`` is ``"int"`` or
    ``"real"`` and ``decimals`` matters only for reals (F/E descriptors)."""

    unit_text: str
    rec: IRExpr
    fields: list[tuple[IRExpr, str, int, int, int]] = field(default_factory=list)
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRDirectWrite:
    """``write(unit, fmt, REC=n) items`` — record-based formatted write.

    The formatted record text is built (the same edit-descriptor chunks a
    sequential formatted write would emit) and placed at record ``rec`` of
    a direct-access file via ``Units::write_record``.  ``format`` is the
    Fortran format string (always present — an unformatted direct write
    is rejected during lowering)."""

    unit_text: str
    rec: IRExpr
    items: list[IRExpr] = field(default_factory=list)
    format: str | None = None
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRInquire:
    """``INQUIRE(...)`` -- query file/unit properties.

    ``selector_kind`` is ``"file"`` or ``"unit"`` (the named selector
    spec); ``selector`` is the lowered file-path or unit-number.
    ``outputs`` is a list of ``(field, IRExpr)`` -- one assignment per
    requested output, where ``field`` is the runtime InquireResult
    member name (``exist``, ``opened``, ``iostat``, ``name``, ...) and
    IRExpr is the lvalue to assign into."""

    selector_kind: str
    selector: IRExpr
    outputs: list[tuple[str, IRExpr]] = field(default_factory=list)
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRFilePosition:
    """``BACKSPACE(u)``, ``REWIND(u)`` -- positional commands on a unit.

    ``op`` is the runtime method name (``"backspace"`` or ``"rewind"``);
    ``unit`` is the lowered unit expression."""

    op: str
    unit: IRExpr
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRUnformattedDirectRead:
    """``read(unit, REC=n) items`` — record-based *unformatted* read.

    The whole record is fetched as raw bytes via ``read_record_raw`` and
    each item is unpacked in declaration order with ``take_bytes`` — C++
    overload resolution dispatches on each item's type (scalar /
    Array<T,R> / FortranString<N>)."""

    unit_text: str
    rec: IRExpr
    items: list[IRExpr] = field(default_factory=list)
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRUnformattedDirectWrite:
    """``write(unit, REC=n) items`` — record-based *unformatted* write.

    Each item is packed onto a byte buffer with ``append_bytes`` (C++
    overload resolution per item type) and the buffer is placed at record
    ``rec`` via ``write_record_raw`` (zero-padded or truncated to RECL)."""

    unit_text: str
    rec: IRExpr
    items: list[IRExpr] = field(default_factory=list)
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
class IRLabel:
    """A statement label (``10 continue``).  Transient: the structuring
    pass consumes every IRLabel, so none survive to emission."""

    label: int
    leading_comments: list[Comment] = field(default_factory=list)
    trailing_comments: list[Comment] = field(default_factory=list)


@dataclass(slots=True)
class IRGoto:
    """A ``goto`` (optionally guarded by ``condition`` — ``if (c) goto
    n``).  Computed GOTO and arithmetic IF lower to a sequence of these.
    Transient: consumed by the structuring pass, never emitted."""

    target: int
    condition: "IRExpr | None" = None
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
class IRPointerAssign:
    """``p => target`` (or nullify).  For a scalar pointer this is
    ``p = &target;``; for an array pointer ``p = target;`` (an ArrayRef
    view).  ``target`` is None for nullify / ``=> null()``."""

    pointer: str
    target: IRExpr | None
    is_array: bool = False
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
class IREntry:
    """An ``ENTRY name(args)`` alternate entry point.  Transient: the
    lowering splits the body at each marker into a standalone subprogram
    (the statements from the entry point onward) and removes the markers,
    so none survive to emission."""

    name: str
    arg_names: tuple[str, ...] = ()
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
    IRDirectRead,
    IRDirectWrite,
    IRUnformattedDirectRead,
    IRUnformattedDirectWrite,
    IRInquire,
    IRFilePosition,
    IRStop,
    IRAllocate,
    IRDeallocate,
    IRPointerAssign,
    IRIf,
    IRDo,
    IRWhile,
    IRSelectCase,
    IRCycle,
    IRExit,
    IRLabel,
    IRGoto,
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

    is_pointer: bool = False
    """True for POINTER locals (scalar -> ``T*``, array -> ArrayRef)."""

    intent: Literal["in", "out", "inout"] | None = None
    """Set when the declaration carried an ``INTENT(...)`` attribute.
    Used by the lowering pass to recognize formal parameters.
    """

    is_optional: bool = False
    """True for ``OPTIONAL`` dummy arguments."""

    common_block: str | None = None
    """Name of the COMMON block this local lives in, when any -- read
    straight off the resolved symbol via the dumper's ``common_block``
    field.  Lets the state-plumbing pass group locals by block without
    re-parsing ``CommonStmt``."""

    equivalence_class: int | None = None
    """0-based index within the owning scope's equivalence-set list, for
    locals that participate in an ``EQUIVALENCE`` statement.  All
    co-aliased locals share the same index.  Read from the dumper's
    ``equivalence_class`` field."""

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

    intent_declared: bool = False
    """True when ``intent`` came from an explicit Fortran ``INTENT(...)``
    declaration (rather than the default).  The whole-program readonly
    inference treats a declared intent as authoritative and skips the
    param — only undeclared dummies (typical FORTRAN 77) are inferred."""

    optional: bool = False
    """True for OPTIONAL dummy args; emitted as ``std::optional<T>`` with
    a ``= std::nullopt`` default (intent(in) scalars)."""

    def cpp_param_type(self) -> str:
        """Just the C++ parameter *type* (the declaration without the
        trailing parameter name)."""
        return self.cpp_param_decl(with_default=False).rsplit(" ", 1)[0]

    def cpp_param_decl(self, *, with_default: bool = True) -> str:
        """C++ parameter declaration string.

        Scalars use a reference (``T&`` or ``const T&`` per intent).
        Arrays use ``fortran::ArrayRef`` by value — ArrayRef is a
        small, non-owning view, so the caller's owning ``Array``
        converts implicitly and the function body can take slices
        without making the caller's storage assumption explicit.

        ``with_default=False`` omits the ``= std::nullopt`` default — a
        C++ default argument may appear only once, so the prototype keeps
        it and the definition drops it.
        """
        if self.optional and not self.type.is_array:
            # intent(in) optional scalar -> std::optional with a default,
            # so trailing optional args can be omitted at the call site.
            default = " = std::nullopt" if with_default else ""
            return f"std::optional<{self.type.cpp}> {self.name}{default}"
        if self.type.is_array:
            # A rank-1 array of assumed-length CHARACTER (element rendered
            # as std::string_view) can't be an ArrayRef<string_view> — the
            # element length is a runtime value, not a C++ type.  Use the
            # character-array view, whose elements are CharRef.
            if (
                self.type.element_type_cpp == "std::string_view"
                and self.type.array_rank == 1
            ):
                return f"fortran::CharArrayRef {self.name}"
            const_q = "const " if self.intent == "in" else ""
            runtime_ref = (
                f"fortran::ArrayRef<{const_q}{self.type.element_type_cpp}, "
                f"{self.type.array_rank}>"
            )
            # When every declared lower bound is a literal integer, encode
            # them in the ArrayRef's ``Lower`` NTTP so indexing in the
            # callee constant-folds.  Callers' arrays (any source Lower)
            # bind via the templated Array->ArrayRef conversion operator
            # and the ArrayRef Lower-rebind constructor.
            from .static_lower import static_lower_cpp_type
            static_ref = static_lower_cpp_type(
                runtime_ref, self.type.array_rank,
                self.type.array_lower_bound_exprs,
            )
            ref_type = static_ref if static_ref is not None else runtime_ref
            decl = f"{ref_type} {self.name}"
            if self.optional and with_default:
                # An OPTIONAL array dummy defaults to a null (empty) view,
                # which PRESENT() reports as absent — so callers can omit it.
                decl += " = {}"
            return decl
        # An assumed-length CHARACTER*(*) dummy is a non-owning character
        # view: ``fortran::CharRef``.  It reads as a string_view, writes
        # through to the caller's storage (for an intent(out/inout) dummy),
        # and supports substring indexing ``s(lo, hi)`` — which a plain
        # ``std::string_view`` does not.  Used for both intent(in) and
        # writable dummies so substrings work uniformly; a read-only dummy
        # simply isn't written.
        if not self.type.is_array and self.type.cpp == "std::string_view":
            return f"fortran::CharRef {self.name}"
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
    """A ``auto& <name> = <param>.<field>;`` reference binding.

    When ``view`` is set, the binding instead reads ``auto <name> =
    <view>;`` -- a by-value view expression rather than a reference to the
    field.  Used for a COMMON member a routine declares with a *different
    shape* than the block's canonical field (storage association): the view
    is an ``ArrayRef`` of the routine's own rank/extents over the shared
    field's storage.
    """

    name: str
    param: str
    field: str
    view: str | None = None


@dataclass(slots=True)
class IREquivMember:
    """One member of an EQUIVALENCE class -- a Fortran name aliased over
    the class's shared byte buffer.  ``cpp_elem_type`` is the C++ scalar
    type (``double``, ``std::int32_t``, ...); ``count`` is the element
    count (None for a scalar slot); ``alignment`` is the natural alignment
    of the element type.  All members share offset 0 in this pass (the
    common SPICE pattern); partial-overlap alignment with explicit element
    indices is rejected during lowering."""

    name: str
    cpp_elem_type: str
    count: int | None
    alignment: int
    is_character: bool = False


@dataclass(slots=True)
class IREquivGroup:
    """One EQUIVALENCE class hoisted to a per-routine local struct.

    Lowering builds one IREquivGroup per ``EQUIVALENCE (a, b, ...)``
    statement, drops the affected IRLocal declarations, and inserts a
    state-binding so the body keeps using the Fortran names (which now
    refer to the typed proxies on the equiv struct).
    """

    cpp_type: str
    """Generated C++ type name (e.g. ``"Foo_Equiv1"``)."""

    byte_size: int
    """Size of the shared byte buffer; max member byte-size."""

    alignment: int
    """``alignas(...)`` value -- max member element alignment."""

    members: list[IREquivMember] = field(default_factory=list)


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

    entry_points: list["IRSubprogram"] = field(default_factory=list)
    """Subprograms synthesized from this unit's ``ENTRY`` statements (one
    per alternate entry point).  Populated during lowering and flattened
    into the translation unit's subprogram list by the collector."""

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

    equiv_groups: list["IREquivGroup"] = field(default_factory=list)
    """EQUIVALENCE classes hoisted to per-routine locals.  One struct
    per group, holding a shared byte buffer plus a typed proxy for each
    aliased Fortran name (see :class:`IREquivGroup`)."""

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
