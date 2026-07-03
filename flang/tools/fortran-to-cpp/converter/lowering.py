"""Walk a flang parse tree and build the converter IR.

Entry point is :func:`lower_program`.  The lowering is intentionally
small and conservative: anything we don't recognize becomes an
``IRUnsupported`` node carrying the original source text, so the
emitter can still produce a compilable C++ skeleton with a clear
``// TODO`` for the user to finish.

Currently supported (v1):

  * ``MainProgram``, ``FunctionSubprogram``, ``SubroutineSubprogram``
  * ``TypeDeclarationStmt`` — integer / real / logical / character
    locals (no arrays yet)
  * ``AssignmentStmt``
  * ``PrintStmt`` with list-directed format
  * ``IfConstruct`` (then / else if / else)
  * ``DoConstruct`` with counted bounds (``do i = lo, hi[, step]``)
  * ``CallStmt`` (statement-level subroutine call)
  * ``ReturnStmt``
  * Numeric / character / logical literals
  * Bare name references
  * Binary operators ``+ - * / // < <= > >= == /= .and. .or.``
"""

from __future__ import annotations

import copy
import dataclasses
import re
from typing import Iterable, Literal, NoReturn

from flang_ast import Comment, Node

from .ir import (
    IRAssignment,
    IRBinaryOp,
    IRCall,
    IRCommonUse,
    IRDo,
    IRExpr,
    IRFunctionCall,
    IRIf,
    IRLiteral,
    IRLocal,
    IRName,
    IRParameter,
    IRPrint,
    IRRaw,
    IRReturn,
    IRStatement,
    IRSubprogram,
    IRTranslationUnit,
    IRType,
    IRUnaryOp,
    IRUnsupported,
)
from .ir import (
    IRAllocate,
    IRArrayConstructor,
    IRBlock,
    IRCast,
    IRCaseClause,
    IRComment,
    IRCycle,
    IRDeallocate,
    IRDerivedType,
    IRDirectRead,
    IRDirectWrite,
    IRUnformattedDirectRead,
    IRUnformattedDirectWrite,
    IRInquire,
    IRFilePosition,
    IREntry,
    IREquivGroup,
    IREquivMember,
    IRStateBinding,
    IRExit,
    IRGoto,
    IRImpliedDo,
    IRLabel,
    IRLambda,
    IRMember,
    IRModule,
    IRPointerAssign,
    IRRead,
    IRSection,
    IRSelectCase,
    IRStop,
    IRSubstr,
    IRTriplet,
    IRWhere,
    IRWhile,
)
from .errors import ConversionError
from .structure import structure_gotos
from .transform import map_expr, map_statement, rename_var
from .types import camelcase, lower_type_spec


# C++ keywords that a lower-cased Fortran identifier could collide with;
# we append ``_`` to keep the generated code compilable (e.g. the
# conventional type-bound-procedure passed object ``this``).
_CPP_KEYWORDS: frozenset[str] = frozenset(
    {
        "alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand",
        "bitor", "bool", "break", "case", "catch", "char", "char8_t",
        "char16_t", "char32_t", "class", "compl", "concept", "const",
        "consteval", "constexpr", "constinit", "const_cast", "continue",
        "co_await", "co_return", "co_yield", "decltype", "default",
        "delete", "do", "double", "dynamic_cast", "else", "enum",
        "explicit", "export", "extern", "false", "float", "for", "friend",
        "goto", "if", "inline", "int", "long", "mutable", "namespace",
        "new", "noexcept", "not", "not_eq", "nullptr", "operator", "or",
        "or_eq", "private", "protected", "public", "register",
        "reinterpret_cast", "requires", "return", "short", "signed",
        "sizeof", "static", "static_assert", "static_cast", "struct",
        "switch", "template", "this", "thread_local", "throw", "true",
        "try", "typedef", "typeid", "typename", "union", "unsigned",
        "using", "virtual", "void", "volatile", "wchar_t", "while",
        "xor", "xor_eq",
    }
)


def _safe_name(fortran: str | None) -> str:
    """Lower-case a Fortran identifier, avoiding C++ keyword clashes."""
    name = (fortran or "").lower()
    return name + "_" if name in _CPP_KEYWORDS else name


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


# Maps a (raw lower-case) callee name to its ordered, safe-named dummy
# argument names.  Built per translation unit and consulted when a call
# uses keyword arguments, so ``f(b=2, a=1)`` can be reordered to the
# positional ``f(1, 2)`` the C++ signature expects.  Each entry is the
# ordered list of ``(safe_dummy_name, is_optional)`` pairs.
_SIGNATURES: dict[str, list[tuple[str, bool]]] = {}

# Per-unit map from a FORMAT statement's label to its format spec string
# (e.g. ``100 format(i3)`` -> {100: "(i3)"}), so ``write(u, 100)`` can be
# resolved to the same format the inline ``write(u, '(i3)')`` form uses.
_FORMAT_LABELS: dict[int, str] = {}


def _build_format_labels(node: Node) -> dict[int, str]:
    out: dict[int, str] = {}

    def rec(n: Node) -> None:
        for c in n.children:
            if c.kind in ("InternalSubprogramPart", "ModuleSubprogramPart"):
                continue  # nested units have their own labels
            if (
                c.kind == "Statement"
                and c.label is not None
                and c.find_first("FormatStmt") is not None
                and c.source is not None
            ):
                spec = _format_spec_from_source(c.source.text)
                if spec is not None:
                    out[c.label] = spec
            rec(c)

    rec(node)
    return out


def _format_spec_from_source(src: str) -> str | None:
    """Pull ``(...)`` out of a ``<label> format(...)`` statement source."""
    m = re.match(r"\s*\d+\s*format\s*(\(.*\))\s*$", src, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m is not None else None


def lower_program(
    root: Node, *, source_file: str | None = None
) -> IRTranslationUnit:
    """Lower an annotated, dependency-ordered parse tree to IR."""
    global _SIGNATURES
    _SIGNATURES = _build_signatures(root)
    tu = IRTranslationUnit(source_file=source_file)
    # Derived-type definitions first (deduplicated by name) so the
    # emitter can declare the structs ahead of everything that uses
    # them.
    seen_types: set[str] = set()
    for node in root.walk():
        if node.kind == "DerivedTypeDef":
            dt = _lower_derived_type_def(node)
            if dt is not None and dt.fortran_name not in seen_types:
                seen_types.add(dt.fortran_name)
                tu.derived_types.append(dt)
    _collect_units(root, tu, parent_module=None)
    _drop_external_function_locals(tu)
    _resolve_component_allocations(tu)
    _reshape_sequence_associated_args(tu)
    _infer_readonly_scalar_params(tu)
    _materialize_value_args(tu)
    _apply_logical_print_format(tu)
    _prepend_block_data_calls_to_main(tu)
    return tu


def _prepend_block_data_calls_to_main(tu: IRTranslationUnit) -> None:
    """Insert ``CALL block_data_init_xyz`` at the top of each main program.

    Fortran ``BLOCK DATA`` is evaluated at load time; in C++ the
    equivalent is to call the synthetic init routines before the user
    code runs.  State plumbing will subsequently thread the touched
    COMMON structs through these calls, so the args resolve.

    A non-main translation unit (a library of subroutines that another
    file's main calls) carries the block_data subprograms but no main of
    its own; the calls are then prepended by the multi-file pipeline
    when the main is found."""
    block_data_names = [s.name for s in tu.subprograms if s.kind == "block_data"]
    if not block_data_names:
        return
    for sub in tu.subprograms:
        if sub.kind != "main":
            continue
        prelude = [
            IRCall(callee=name, args=[]) for name in block_data_names
        ]
        sub.body = prelude + sub.body


def _drop_external_function_locals(tu: IRTranslationUnit) -> None:
    """Drop a scalar local whose name is a function defined in the program.

    A routine that calls an external function declares only the
    function's *result type* (``REAL INTERP``); lowered naively this
    becomes a scalar local that shadows the function, so the call
    ``interp(...)`` is rejected as "interp cannot be used as a function".
    Such a declaration isn't a variable — drop it so the call binds the
    function (and its prototype).

    Only drop when the routine *actually calls* that name: a local that
    merely shares a name with some unrelated function elsewhere in the
    program (e.g. a CHARACTER ``fout`` used to build a filename, when
    another file defines a function ``fout``) is a real variable."""
    func_names = {s.name for s in tu.subprograms if s.kind == "function"}
    if not func_names:
        return
    # Union the called-names across each ENTRY group: an entry that doesn't
    # itself call the function still declares its result type as a spurious
    # local, and once the group shares one SAVE struct that local would be
    # bound for *every* sibling -- shadowing the function for the entries
    # that do call it.  Dropping it group-wide keeps the call site valid.
    group_called: dict[str, set[str]] = {}
    for sub in tu.subprograms:
        key = sub.entry_group or sub.name
        group_called.setdefault(key, set()).update(_called_names(sub.body))
    for sub in tu.subprograms:
        param_names = {p.name for p in sub.parameters}
        called = group_called[sub.entry_group or sub.name]
        sub.locals = [
            loc
            for loc in sub.locals
            if not (
                loc.name in func_names
                and loc.name in called
                and loc.name != sub.name
                and loc.name not in param_names
                and not loc.type.is_array
            )
        ]


def _called_names(body: list[IRStatement]) -> set[str]:
    """Names invoked as a function or subroutine anywhere in ``body``."""
    out: set[str] = set()

    def on_expr(e: IRExpr) -> IRExpr:
        if isinstance(e, IRFunctionCall):
            out.add(e.callee)
        return e

    def on_stmt(s: IRStatement) -> IRStatement:
        if isinstance(s, IRCall):
            out.add(s.callee)
        return s

    for s in body:
        map_statement(s, on_stmt=on_stmt, on_expr=lambda e: map_expr(e, on_expr))
    return out


def _infer_readonly_scalar_params(tu: IRTranslationUnit) -> None:
    """Mark an ``inout`` scalar dummy that the body never modifies as
    ``intent(in)`` (``const T&``).

    FORTRAN 77 has no INTENT, so every dummy defaults to ``inout`` /
    ``T&`` — which can't bind an rvalue, so a call like ``eptr(x, y, z*2)``
    fails.  A scalar dummy can be ``const T&`` (intent(in)) when the body
    never modifies it: it's not assigned, a DO variable, or read into, and
    every call that receives it passes it to a parameter that is itself
    read-only.  That last condition is cross-procedural (``interp`` passes
    ``n`` to ``locate``, which only reads it), so this is solved as a
    fixpoint: a parameter is non-const if it's written locally or passed
    to an already-non-const callee parameter.  Passing to an unknown
    callee is conservatively a write.  A wrong guess is a compile error,
    never silent misbehavior.
    """
    by_name = {s.name: s for s in tu.subprograms}

    def root(e: IRExpr) -> str | None:
        if isinstance(e, IRName):
            return e.name
        if isinstance(e, IRMember):
            return root(e.base)
        if isinstance(e, IRFunctionCall):
            return e.callee.split(".", 1)[0]
        if isinstance(e, IRSection):
            return e.array.split(".", 1)[0]
        return None

    # Per subprogram: which scalar params are inout candidates, which are
    # written locally (definitely non-const), and the call edges
    # (param passed as the idx-th argument of callee).
    scalar_inout: dict[str, set[str]] = {}
    written: dict[str, set[str]] = {}
    edges: dict[str, list[tuple[str, str, int]]] = {}
    non_const: set[tuple[str, str]] = set()

    for sub in tu.subprograms:
        pnames = {p.name for p in sub.parameters}
        # Dummy *procedure* parameters: a routine that takes a callback
        # and calls it on one of its own dummies has an opaque write
        # surface (the actual procedure's body is bound at the call
        # site, and any actual we don't have the source for could write
        # the arg).  Conservatively treat such calls as local writes
        # below.  The fixpoint then propagates non-const-ness back up
        # through every caller that passes its own param into that
        # dummy.
        proc_dummy_names = {
            p.name for p in sub.parameters if p.type.is_procedure
        }
        # Any scalar (non-array, non-pointer) param is a const candidate —
        # including one a prior (per-file) run already marked ``in``, so a
        # later whole-program run can correct it back to ``inout`` once a
        # cross-file callee that writes it becomes visible.  But a param
        # whose ``intent`` was *declared* by the source (F90+
        # ``INTENT(...)`` or a standalone ``INTENT(...) :: x`` statement) is
        # authoritative — flang has consolidated it and the converter must
        # not demote a declared ``inout`` to ``in`` just because this body
        # happens not to write it.  Inference covers FORTRAN 77 dummies
        # (no INTENT) only.
        scalar_inout[sub.name] = {
            p.name
            for p in sub.parameters
            if p.intent in ("in", "inout")
            and not p.intent_declared
            and not p.type.is_array
            and not p.type.is_pointer
            and not p.type.is_procedure
        }
        w: set[str] = set()
        e: list[tuple[str, str, int]] = []

        def handle_call(
            callee: str, args, *, _e=e, _pn=pnames, _w=w,
            _proc_dummies=proc_dummy_names,
        ) -> None:
            # A call through a *dummy procedure* parameter has an opaque
            # body: at the C++ template instantiation site any actual --
            # including one we don't have the source of -- could write
            # the arg.  Conservatively mark each scalar param passed
            # positionally as locally written, exactly like an
            # ``IRAssignment(target=p)`` would.
            if callee in _proc_dummies:
                for a in args:
                    if isinstance(a, IRName) and a.name in _pn:
                        _w.add(a.name)
                return
            # Only a known user subprogram creates a dependency edge.  A
            # call-shaped node with an unknown callee is array indexing
            # (``apl(i,ic)`` — read-only subscripts) or an intrinsic
            # (pure); neither modifies its arguments.
            if callee not in by_name:
                return
            for idx, a in enumerate(args):
                if isinstance(a, IRName) and a.name in _pn:
                    _e.append((a.name, callee, idx))

        def note_stmt(stmt: IRStatement, *, _w=w) -> IRStatement:
            if isinstance(stmt, IRAssignment):
                r = root(stmt.target)
                if r is not None:
                    _w.add(r)
            elif isinstance(stmt, IRDo) and stmt.var:
                _w.add(stmt.var)
            elif isinstance(stmt, IRRead):
                for it in stmt.items:
                    r = root(it)
                    if r is not None:
                        _w.add(r)
            elif isinstance(stmt, IRDirectRead):
                for tgt, *_rest in stmt.fields:
                    r = root(tgt)
                    if r is not None:
                        _w.add(r)
            elif isinstance(stmt, IRUnformattedDirectRead):
                for it in stmt.items:
                    r = root(it)
                    if r is not None:
                        _w.add(r)
            elif isinstance(stmt, IRInquire):
                for _f, tgt in stmt.outputs:
                    r = root(tgt)
                    if r is not None:
                        _w.add(r)
            elif isinstance(stmt, IRCall):
                handle_call(stmt.callee, stmt.args)
            return stmt

        def note_expr(expr: IRExpr) -> IRExpr:
            if isinstance(expr, IRFunctionCall):
                handle_call(expr.callee, expr.args)
            return expr

        for s in sub.body:
            map_statement(
                s, on_stmt=note_stmt, on_expr=lambda e2: map_expr(e2, note_expr)
            )
        written[sub.name] = w
        edges[sub.name] = e
        for pn in scalar_inout[sub.name] & w:
            non_const.add((sub.name, pn))

    # Fixpoint: a candidate becomes non-const if it's passed to a callee
    # parameter that is (now) non-const.
    changed = True
    while changed:
        changed = False
        for sname, elist in edges.items():
            for pname, callee, idx in elist:
                if (sname, pname) in non_const:
                    continue
                if pname not in scalar_inout[sname]:
                    continue
                cparams = by_name[callee].parameters
                if idx < len(cparams) and (callee, cparams[idx].name) in non_const:
                    non_const.add((sname, pname))
                    changed = True

    for sub in tu.subprograms:
        for p in sub.parameters:
            if p.name in scalar_inout[sub.name]:
                p.intent = (
                    "inout" if (sub.name, p.name) in non_const else "in"
                )


def _expr_rank(expr: IRExpr) -> int | None:
    """The array rank of an expression where it can be told cheaply: a
    section's rank is its number of triplet subscripts.  Returns ``None``
    when unknown (so callers leave the argument untouched)."""
    if isinstance(expr, IRSection):
        return sum(1 for s in expr.subscripts if isinstance(s, IRTriplet))
    return None


def _is_array_element(
    expr: IRExpr, subprograms: dict, local_arrays: "set[str] | None" = None
) -> bool:
    """True if ``expr`` indexes an array (``a(i)``, ``v%c(i)``) rather than
    calls a function.  An array access lowers to a call-shaped node whose
    callee is an access path, not a known subprogram or a ``ftn::`` /
    ``std::`` intrinsic.

    A local or dummy array *shadows* a same-named global subprogram (SPICE
    has both a ``POS`` function and routines with a dummy array ``POS(3)``);
    when ``local_arrays`` names such an array, the access wins over the
    global subprogram so the sequence-association reshape still fires."""
    if not (isinstance(expr, IRFunctionCall) and "::" not in expr.callee
            and len(expr.args) >= 1):
        return False
    if local_arrays is not None and expr.callee in local_arrays:
        return True
    return expr.callee not in subprograms


def _reshape_sequence_associated_args(tu: IRTranslationUnit) -> None:
    """Fortran sequence association: a contiguous rank-1 actual passed to
    a higher-rank, explicit-shape dummy.  Wrap such an actual in
    ``ftn::seq_assoc<R>(..., {lowers}, {extents})`` using the dummy's
    declared shape, so the call type-checks.  Runs before state plumbing
    so call arguments still line up with the callee's Fortran dummies."""
    params_by_name = {s.name: s.parameters for s in tu.subprograms}
    # PARAMETER locals (compile-time constants) of each subprogram, keyed
    # by routine name and PARAMETER name -> rendered initializer text.
    # Used by _subst_dummy_bounds to fold a callee-local PARAMETER
    # referenced by a dummy's declared bound (``DIMENSION X(MXCOMP)``)
    # into the literal value so the emitted ``elem_tail_n(... ext ...)``
    # at the caller site doesn't reference a name from the callee's
    # scope.
    parameter_consts_by_name: dict[str, dict[str, str]] = {}
    for sub in tu.subprograms:
        consts: dict[str, str] = {}
        for loc in sub.locals:
            if loc.is_parameter and loc.initializer is not None:
                from .emit import _render_expr
                try:
                    consts[loc.name] = _render_expr(loc.initializer)
                except Exception:
                    continue
        if consts:
            parameter_consts_by_name[sub.name] = consts

    def _subst_dummy_bounds(
        exprs: list[str], params: list, out: list[IRExpr],
        callee: str = "",
    ) -> list[str]:
        """Rewrite a dummy's declared bound expressions (which reference the
        *callee's* other dummy parameters, e.g. ``VALUES(NCOLS, N)``, or
        the callee's local ``PARAMETER`` constants, e.g.
        ``TVEC(MXCOMP)``) into the *caller's* scope by substituting each
        referenced name with the actual or constant value at this call
        site."""
        from .emit import _render_expr

        name_to_text: dict[str, str] = {}
        for j, q in enumerate(params):
            if j < len(out) and not q.type.is_array:
                name_to_text[q.name] = f"({_render_expr(out[j])})"
        # Fold in callee-local PARAMETER constants.  Dummy-parameter
        # substitutions win on a name collision (dummies shadow locals).
        for nm, txt in parameter_consts_by_name.get(callee, {}).items():
            name_to_text.setdefault(nm, f"({txt})")
        if not name_to_text:
            return exprs
        return [
            re.sub(
                r"[A-Za-z_]\w*",
                lambda m: name_to_text.get(m.group(0), m.group(0)),
                e,
            )
            for e in exprs
        ]

    def reshape(
        callee: str, args: list[IRExpr], caller_arrays: dict[str, int]
    ) -> list[IRExpr]:
        params = params_by_name.get(callee)
        if not params:
            return args
        out = list(args)
        for i, p in enumerate(params):
            if i >= len(out):
                break
            actual = out[i]
            if not p.type.is_array:
                # Whole-array actual passed to a *scalar* dummy: the dummy
                # is storage-associated with the array's first element.
                if (
                    not p.type.is_procedure
                    and isinstance(actual, IRName)
                    and actual.name in caller_arrays
                ):
                    out[i] = IRFunctionCall(
                        callee="ftn::first", args=(actual,)
                    )
                continue
            actual_rank1 = _expr_rank(actual) == 1 or (
                isinstance(actual, IRName) and caller_arrays.get(actual.name) == 1
            )
            if (
                p.type.array_rank >= 2
                and p.type.array_extent_exprs
                and actual_rank1
            ):
                rank = p.type.array_rank
                lowers = list(p.type.array_lower_bound_exprs or ["1"] * rank)
                extents = list(p.type.array_extent_exprs)
                # The dummy's bounds may reference the callee's *other*
                # dummies (``VALUES(NCOLS, N)``); rewrite those names into
                # the actual arguments passed at this call site so the
                # ``{...}`` extent list is valid in the caller's scope.
                lowers = _subst_dummy_bounds(lowers, params, out, callee)
                extents = _subst_dummy_bounds(extents, params, out, callee)
                # An assumed-size dummy (``A(M, *)``) has a placeholder for
                # the trailing ``*`` extent; for a sequence-associated 1-D
                # actual, that dim spans the rest of the actual's storage:
                # ``actual.size() / (product of the leading extents)``.
                if extents and "assumed" in extents[-1]:
                    leading = extents[:-1]
                    prod = (
                        " * ".join(f"({e})" for e in leading) if leading else "1"
                    )
                    extents[-1] = (
                        f"({_render_expr_inline(actual)}.size()) / ({prod})"
                    )
                out[i] = IRFunctionCall(
                    callee=f"ftn::seq_assoc<{rank}>",
                    args=(
                        actual,
                        IRRaw("{" + ", ".join(lowers) + "}"),
                        IRRaw("{" + ", ".join(extents) + "}"),
                    ),
                )
            elif (
                p.type.array_rank >= 2
                and p.type.array_extent_exprs
                and _is_array_element(actual, params_by_name, set(caller_arrays))
            ):
                # ``call mxm(.., ref(1,1,i))`` -- a multi-dimensional array
                # *element* to a higher-rank explicit-shape dummy.  View the
                # storage from that element with the dummy's bounds (the
                # rank>=2 analogue of the elem_tail element-to-1D case).
                rank = p.type.array_rank
                lowers = _subst_dummy_bounds(
                    list(p.type.array_lower_bound_exprs or ["1"] * rank),
                    params, out, callee,
                )
                extents = _subst_dummy_bounds(
                    list(p.type.array_extent_exprs), params, out, callee
                )
                if not any("assumed" in e for e in extents):
                    out[i] = IRFunctionCall(
                        callee=f"ftn::seq_assoc_at<{rank}>",
                        args=(
                            IRName(name=actual.callee, fortran=actual.callee),
                            IRRaw("{" + ", ".join(lowers) + "}"),
                            IRRaw("{" + ", ".join(extents) + "}"),
                            *actual.args,
                        ),
                    )
                else:
                    # Assumed-size dummy (``PLATES(3, *)``): the leading
                    # extents are real, the trailing ``*`` spans the rest of
                    # the actual's storage from this element -- computed at
                    # runtime.  Pass the leading extents with a 0 placeholder
                    # for the trailing dim, which ``seq_assoc_at_rest`` fills.
                    rest_extents = [
                        "0" if "assumed" in e else e for e in extents
                    ]
                    out[i] = IRFunctionCall(
                        callee=f"ftn::seq_assoc_at_rest<{rank}>",
                        args=(
                            IRName(name=actual.callee, fortran=actual.callee),
                            IRRaw("{" + ", ".join(lowers) + "}"),
                            IRRaw("{" + ", ".join(rest_extents) + "}"),
                            *actual.args,
                        ),
                    )
            elif p.type.array_rank == 1 and _is_array_element(
                actual, params_by_name, set(caller_arrays)
            ):
                # ``call s(a(i,j))`` with an array dummy: the dummy views
                # the storage from that element onward (sequence assoc).
                # When the dummy has an explicit extent (``DIMENSION X(7)``)
                # use it for the view size -- the callee's internal
                # indexing into x(1..7) needs that bound, even when the
                # caller's source array has fewer than 7 elements behind
                # the chosen base.  Falls back to ``elem_tail`` (remaining
                # storage) for assumed-shape / unknown-extent dummies.
                extent_exprs = _subst_dummy_bounds(
                    list(p.type.array_extent_exprs or ()), params, out, callee
                )
                # An assumed-size ``ARRAY(*)`` dummy has no real extent -- its
                # placeholder is ``/* assumed-size */ 0``; using it as the
                # view size makes an empty view, so any callee index overruns
                # (the SPICE MOVED/MOVEI(.., SUM(N+1)) crashes).  Only use a
                # *concrete* dummy extent; otherwise view the remaining
                # storage with ``elem_tail``.
                concrete_extent = (
                    extent_exprs
                    and len(extent_exprs) == 1
                    and "assumed" not in extent_exprs[0]
                )
                if concrete_extent:
                    out[i] = IRFunctionCall(
                        callee="ftn::elem_tail_n",
                        args=(
                            IRName(name=actual.callee, fortran=actual.callee),
                            IRRaw(extent_exprs[0]),
                            *actual.args,
                        ),
                    )
                else:
                    out[i] = IRFunctionCall(
                        callee="ftn::elem_tail",
                        args=(
                            IRName(name=actual.callee, fortran=actual.callee),
                            *actual.args,
                        ),
                    )
        return out

    for sub in tu.subprograms:
        caller_arrays = {
            loc.name: loc.type.array_rank for loc in sub.locals if loc.type.is_array
        }
        caller_arrays.update(
            {p.name: p.type.array_rank for p in sub.parameters if p.type.is_array}
        )

        def fix_stmt(stmt: IRStatement) -> IRStatement:
            if isinstance(stmt, IRCall):
                return IRCall(
                    callee=stmt.callee,
                    args=reshape(stmt.callee, list(stmt.args), caller_arrays),
                    leading_comments=stmt.leading_comments,
                    trailing_comments=stmt.trailing_comments,
                )
            return stmt

        def fix_expr(expr: IRExpr) -> IRExpr:
            if isinstance(expr, IRFunctionCall):
                return IRFunctionCall(
                    callee=expr.callee,
                    args=tuple(reshape(expr.callee, list(expr.args), caller_arrays)),
                )
            return expr

        sub.body = [
            map_statement(
                s, on_stmt=fix_stmt, on_expr=lambda e: map_expr(e, fix_expr)
            )
            for s in sub.body
        ]


def _resolve_component_allocations(tu: IRTranslationUnit) -> None:
    """Resolve the cpp_type of ``allocate`` targets that are derived-type
    components (``allocate(subset%beta(...))``).  The per-subprogram
    :func:`_resolve_allocations` only knows local names; a component's
    type comes from the owning derived type's field, so it is resolved
    here once all derived types and subprograms are lowered."""
    dt_by_cpp = {dt.cpp_type: dt for dt in tu.derived_types}
    if not dt_by_cpp:
        return
    for sub in tu.subprograms:
        local_type = {loc.name: loc.type for loc in sub.locals}
        for p in sub.parameters:
            local_type.setdefault(p.name, p.type)

        def fix(stmt: IRStatement) -> IRStatement:
            if (
                isinstance(stmt, IRAllocate)
                and not stmt.cpp_type
                and "." in stmt.obj
            ):
                base, _, field = stmt.obj.partition(".")
                base_type = local_type.get(base)
                dt = dt_by_cpp.get(base_type.cpp) if base_type else None
                if dt is not None:
                    for f in dt.fields:
                        if f.name == field:
                            stmt.cpp_type = f.type.cpp
                            break
            return stmt

        sub.body = [map_statement(s, on_stmt=fix) for s in sub.body]


# Operators whose result is logical, so a list-directed print item built
# from them must render as Fortran ``T`` / ``F``.
_LOGICAL_RESULT_OPS = frozenset(
    {"==", "!=", "<", "<=", ">", ">=", "&&", "||"}
)


def _apply_logical_print_format(tu: IRTranslationUnit) -> None:
    """Wrap logical items of list-directed ``print`` in ``ftn::
    logical_text`` so they render as Fortran ``T`` / ``F`` rather than
    C++'s default ``1`` / ``0``."""
    for sub in tu.subprograms:
        logical_names = {
            loc.name
            for loc in sub.locals
            if loc.type.is_logical and not loc.type.is_array
        }
        logical_names |= {
            p.name
            for p in sub.parameters
            if p.type.is_logical and not p.type.is_array
        }

        def fix(stmt: IRStatement) -> IRStatement:
            if isinstance(stmt, IRPrint) and stmt.format is None:
                stmt.items = [
                    IRFunctionCall(callee="ftn::logical_text", args=(it,))
                    if _is_logical_expr(it, logical_names)
                    else it
                    for it in stmt.items
                ]
            return stmt

        sub.body = [map_statement(s, on_stmt=fix) for s in sub.body]


def _materialize_value_args(tu: IRTranslationUnit) -> None:
    """Pass a constant/expression actual through ``ftn::byref`` when
    the dummy is a modifiable scalar reference.

    Fortran lets any expression be an actual argument; for an INOUT/OUT
    dummy the compiler binds a temporary (copy-in, write-back discarded).
    C++ won't bind a non-const ``T&`` to an rvalue, so ``call s(x, 0.0)``
    fails to compile.  ``ftn::byref`` materializes the value into an
    lvalue whose lifetime spans the call, restoring Fortran's behavior
    (it is a no-op for an lvalue actual, which still binds directly).

    Rvalue-ness comes from the AST ``category`` flang attached to each
    actual ``Expr`` (``"variable"`` = assignable designator, anything else
    = rvalue).  Some call sites have no category populated (synthetic
    helpers, intrinsics that build their own arg list); for those, fall
    back to recognizing an unambiguous-rvalue IR node shape — a literal,
    arithmetic / relational result, cast, or call to a user function."""
    by_name = {s.name: s for s in tu.subprograms}
    if not by_name:
        return
    # Fallback set for actuals with no AST-derived category: an IR shape
    # known to denote an rvalue.  Designators (names, array elements,
    # sections, components) are not in this set — they bind to a reference.
    rvalue_nodes = (IRLiteral, IRBinaryOp, IRUnaryOp, IRCast)

    def is_value_call(a: IRExpr) -> bool:
        """A call to a user *function* — its result is an rvalue, unlike an
        array-element access (callee not a subprogram) or a reshape helper
        (``ftn::first`` etc.) which yield references."""
        return (
            isinstance(a, IRFunctionCall)
            and a.callee in by_name
            and by_name[a.callee].kind == "function"
        )

    def is_rvalue(a: IRExpr, cat: str) -> bool:
        # Authoritative when the AST gave us a category; ``variable`` =
        # designator with storage, anything else (``constant`` /
        # ``expression``) is an rvalue.
        if cat:
            return cat != "variable"
        return isinstance(a, rvalue_nodes) or is_value_call(a)

    def wants_ref(callee: str, i: int) -> bool:
        sub = by_name.get(callee)
        if sub is None or i >= len(sub.parameters):
            return False
        p = sub.parameters[i]
        return (
            not p.type.is_array
            and not p.type.is_procedure
            and not p.optional
            and p.intent != "in"
        )

    def wrap(
        callee: str, args, cats: tuple[str, ...], const_names: set[str]
    ) -> list:
        out = []
        for i, a in enumerate(args):
            cat = cats[i] if i < len(cats) else ""
            if is_rvalue(a, cat) and wants_ref(callee, i):
                out.append(IRFunctionCall(callee="ftn::byref", args=(a,)))
            elif (
                isinstance(a, IRName)
                and a.name in const_names
                and wants_ref(callee, i)
            ):
                # A PARAMETER constant (a const local) passed to a modifiable
                # dummy: bind a writable copy (Fortran copy-in; write-back
                # discarded).  ``byref`` alone keeps the const, so copy first.
                out.append(
                    IRFunctionCall(
                        callee="ftn::byref",
                        args=(IRFunctionCall(callee="ftn::val", args=(a,)),),
                    )
                )
            else:
                out.append(a)
        return out

    for sub in tu.subprograms:
        const_names = {loc.name for loc in sub.locals if loc.is_parameter}

        def on_expr(e: IRExpr) -> IRExpr:
            if isinstance(e, IRFunctionCall) and e.callee in by_name:
                return IRFunctionCall(
                    callee=e.callee,
                    args=tuple(wrap(e.callee, e.args, e.arg_categories, const_names)),
                    arg_categories=e.arg_categories,
                )
            return e

        def on_stmt(s: IRStatement) -> IRStatement:
            if isinstance(s, IRCall) and s.callee in by_name:
                s.args = wrap(s.callee, s.args, s.arg_categories, const_names)
            return s

        sub.body = [
            map_statement(st, on_stmt=on_stmt, on_expr=lambda e: map_expr(e, on_expr))
            for st in sub.body
        ]


def _is_logical_expr(expr: IRExpr, logical_names: set[str]) -> bool:
    if isinstance(expr, IRName):
        return expr.name in logical_names
    if isinstance(expr, IRBinaryOp):
        return expr.op in _LOGICAL_RESULT_OPS
    if isinstance(expr, IRUnaryOp):
        if expr.op == "()":  # parentheses: logical iff the operand is
            return _is_logical_expr(expr.operand, logical_names)
        return expr.op == "!"
    if isinstance(expr, IRLiteral):
        return expr.cpp_text in ("true", "false")
    return False


def _build_signatures(root: Node) -> dict[str, list[tuple[str, bool]]]:
    """Map every subprogram's (raw lower-case) name to its ordered dummy
    arguments as ``(safe_name, is_optional)`` pairs, for keyword-argument
    reordering and omitted-optional filling."""
    sigs: dict[str, list[tuple[str, bool]]] = {}
    for node in root.walk():
        if node.kind == "SubroutineSubprogram":
            name = _extract_subprogram_name(node, "SubroutineStmt")
            if name:
                sigs[name.lower()] = _with_optionality(
                    node, _extract_subroutine_dummy_args(node)
                )
        elif node.kind == "FunctionSubprogram":
            name = _extract_subprogram_name(node, "FunctionStmt")
            if name:
                sigs[name.lower()] = _with_optionality(
                    node, _extract_function_dummy_args(node)
                )
    return sigs


def _with_optionality(
    subprog: Node, names: list[str]
) -> list[tuple[str, bool]]:
    optional = _optional_dummy_names(subprog)
    return [(n, n in optional) for n in names]


def _optional_dummy_names(subprog: Node) -> set[str]:
    """Safe-names of the dummy arguments declared ``OPTIONAL``."""
    names: set[str] = set()
    spec = subprog.find_first("SpecificationPart")
    if spec is None:
        return names
    for decl in spec.find_all("TypeDeclarationStmt"):
        has_optional = any(
            child.kind == "Optional"
            for attr in decl.find_all("AttrSpec")
            for child in attr.children
        )
        if not has_optional:
            continue
        for ent in decl.find_all("EntityDecl"):
            nm = ent.find_first("Name")
            if nm is not None and nm.fortran:
                names.add(_safe_name(nm.fortran))
    return names


def _collect_units(
    node: Node,
    tu: IRTranslationUnit,
    *,
    parent_module: str | None,
    inherited_uses: tuple[str, ...] = (),
) -> None:
    """Recursively collect modules and subprograms, tracking the
    enclosing module so module procedures know their host.

    ``inherited_uses`` are the modules ``USE``d by the enclosing module;
    its contained procedures host-associate those names, so they are
    folded into each procedure's ``used_modules`` (e.g. a module ``USE``s
    a constants module, and its procedures reference those constants)."""
    for child in node.children:
        kind = child.kind
        if kind == "Module":
            _collect_module(child, tu)
        elif kind == "MainProgram":
            # Internal procedures (the program's CONTAINS section) become
            # free functions; collect them first so a callee is emitted
            # before its host caller.
            _collect_internal_subprograms(child, tu, parent_module, inherited_uses)
            tu.subprograms.append(_lower_main_program(child))
        elif kind == "FunctionSubprogram":
            _collect_internal_subprograms(child, tu, parent_module, inherited_uses)
            sub = _lower_function(child)
            sub.parent_module = parent_module
            _add_inherited_uses(sub, inherited_uses)
            _append_with_entries(tu, sub, inherited_uses)
        elif kind == "SubroutineSubprogram":
            _collect_internal_subprograms(child, tu, parent_module, inherited_uses)
            sub = _lower_subroutine(child)
            sub.parent_module = parent_module
            _add_inherited_uses(sub, inherited_uses)
            _append_with_entries(tu, sub, inherited_uses)
        elif kind == "BlockData":
            # BLOCK DATA: a Fortran load-time initializer for COMMON
            # blocks.  Lower it as a synthetic init routine; the main
            # program calls it before the body runs so the COMMON
            # struct fields hold the initialized values that GTS7's
            # coefficient tables / similar code depend on.
            sub = _lower_block_data(child)
            if sub is not None:
                tu.subprograms.append(sub)
        else:
            # Descend through containers (Program, ProgramUnit,
            # ModuleSubprogramPart, ModuleSubprogram, ...).
            _collect_units(
                child,
                tu,
                parent_module=parent_module,
                inherited_uses=inherited_uses,
            )


def _append_with_entries(
    tu: IRTranslationUnit, sub: IRSubprogram, inherited_uses: tuple[str, ...]
) -> None:
    """Append ``sub`` and any subprograms its ENTRY statements produced;
    the entries inherit the same module/host context as their unit."""
    tu.subprograms.append(sub)
    for entry in sub.entry_points:
        entry.parent_module = sub.parent_module
        _add_inherited_uses(entry, inherited_uses)
        tu.subprograms.append(entry)
    sub.entry_points = []


def _add_inherited_uses(sub: "IRSubprogram", inherited_uses: tuple[str, ...]) -> None:
    for mod in inherited_uses:
        if mod not in sub.used_modules:
            sub.used_modules.append(mod)


def _collect_internal_subprograms(
    host: Node,
    tu: IRTranslationUnit,
    parent_module: str | None,
    inherited_uses: tuple[str, ...] = (),
) -> None:
    """Collect a host unit's ``InternalSubprogramPart`` procedures."""
    for child in host.children:
        if child.kind == "InternalSubprogramPart":
            _collect_units(
                child,
                tu,
                parent_module=parent_module,
                inherited_uses=inherited_uses,
            )


def _collect_module(mod_node: Node, tu: IRTranslationUnit) -> None:
    name = ""
    for stmt in mod_node.children:
        if stmt.kind == "Statement":
            ms = stmt.find_first("ModuleStmt")
            if ms is not None:
                nm = ms.find_first("Name")
                if nm is not None and nm.fortran:
                    name = nm.fortran
                break
    if not name:
        return
    module = IRModule(
        cpp_type=camelcase(name) + "Module",
        fortran_name=_safe_name(name),
    )
    # Module-level variable declarations live in the module's direct
    # SpecificationPart; so do its own ``use`` statements.
    module_uses: tuple[str, ...] = ()
    for child in mod_node.children:
        if child.kind == "SpecificationPart":
            module.variables = _lower_specification(child)
            module_uses = tuple(_lower_use_statements(child))
    tu.modules.append(module)
    # Module procedures (in the CONTAINS section) are collected with this
    # module as their host and inherit the module's ``use`` imports.
    for child in mod_node.children:
        if child.kind == "ModuleSubprogramPart":
            _collect_units(
                child,
                tu,
                parent_module=module.fortran_name,
                inherited_uses=module_uses,
            )


def _lower_derived_type_def(node: Node) -> "IRDerivedType | None":
    """Lower a ``DerivedTypeDef`` to an IRDerivedType."""
    type_name = ""
    for stmt in node.children:
        if stmt.kind == "Statement":
            dts = stmt.find_first("DerivedTypeStmt")
            if dts is not None:
                name = dts.find_first("Name")
                if name is not None and name.fortran:
                    type_name = name.fortran
                break
    if not type_name:
        return None
    fields: list[IRLocal] = []
    for comp in node.find_all("DataComponentDefStmt"):
        type_node = comp.first_child("DeclarationTypeSpec")
        if type_node is None:
            continue
        comp_type = lower_type_spec(type_node)
        for decl in comp.find_all("ComponentDecl"):
            name = decl.first_child("Name")
            if name is not None and name.fortran:
                # A component may carry its own array spec (component
                # array).  Its shape node is a ``ComponentArraySpec``
                # rather than the ``ArraySpec`` used for ordinary locals.
                arr = decl.first_child("ArraySpec") or decl.first_child(
                    "ComponentArraySpec"
                )
                field_type = (
                    _make_array_type(comp_type, arr) if arr is not None
                    else comp_type
                )
                init = decl.find_first("Initialization")
                init_expr = None
                if init is not None:
                    e = init.find_first("Expr") or init.find_first("ConstantExpr")
                    if e is not None:
                        init_expr = _lower_expression(e)
                fields.append(
                    IRLocal(
                        name=_safe_name(name.fortran),
                        type=field_type,
                        initializer=init_expr,
                    )
                )
    return IRDerivedType(
        cpp_type=camelcase(type_name),
        fortran_name=type_name.lower(),
        fields=fields,
    )


# ---------------------------------------------------------------------------
# Subprograms
# ---------------------------------------------------------------------------


def _subprogram_leading_comments(node: Node, stmt_kind: str) -> list[Comment]:
    """Pull a subprogram's header doc-comments from its parse tree.

    Comments **above** a ``SUBROUTINE`` / ``FUNCTION`` / ``PROGRAM`` /
    ``BLOCK DATA`` statement are attached by the annotator to the first
    child ``Statement`` node (the one containing the *Stmt) since the
    outer subprogram-subprogram node has no source range.  Comments
    **below** the *Stmt and above the first declaration form the
    routine's "header" doc-block (think of ``! Compute ...`` after
    ``subroutine s()``); the annotator hangs those on the first
    ``DeclarationConstruct``'s inner ``Statement`` node.  We
    concatenate the two so the emitter writes them as one comment block
    above the routine's C++ definition.
    """
    out: list[Comment] = []
    out.extend(node.leading_comments)

    # Comments above the *Stmt land on the Statement child that wraps it.
    header_stmt: Node | None = None
    for child in node.children:
        if child.kind == "Statement" and child.first_child(stmt_kind) is not None:
            header_stmt = child
            out.extend(child.leading_comments)
            break

    # Comments between the *Stmt and the first decl land on the inner
    # Statement of the first DeclarationConstruct in the SpecificationPart.
    for child in node.children:
        if child.kind != "SpecificationPart":
            continue
        for spec in child.children:
            inner_stmt = spec.find_first("Statement") if spec is not None else None
            if inner_stmt is not None and inner_stmt is not header_stmt:
                out.extend(inner_stmt.leading_comments)
                return out
        break
    return out


def _lower_main_program(node: Node) -> IRSubprogram:
    """Lower a ``PROGRAM`` unit to an ``IRSubprogram`` with ``kind="main"``.

    The Fortran main program body is emitted as a free function (named
    after the program, defaulting to ``"main_program"`` if the
    ``PROGRAM`` statement was omitted).  A small ``int main()`` wrapper
    later in :mod:`emit` calls it — this keeps the "every unit ↦ one
    function" invariant and lets state plumbing treat the main program
    exactly like any subroutine."""
    name = _extract_subprogram_name(node, "ProgramStmt") or "main_program"
    body_name = _safe_name(name)
    # The generated C++ entry point is ``int main()``; a Fortran program
    # literally named ``main`` would collide with it, so give the body a
    # distinct, non-colliding name.
    if body_name == "main":
        body_name = "main_program"
    sub = IRSubprogram(
        name=body_name,
        display_name=name,
        kind="main",
        leading_comments=_subprogram_leading_comments(node, "ProgramStmt"),
        source=node.source,
    )
    _lower_specification_and_execution(node, sub)
    return sub


def _lower_function(node: Node) -> IRSubprogram:
    """Lower a ``FUNCTION`` subprogram to an ``IRSubprogram``.

    Reads the dummy-arg list from the ``FunctionStmt``, lowers the
    specification + execution parts, separates parameters from locals,
    then lifts the function-named local into the C++ return value.  The
    return type comes from the function-defining Name's resolved symbol
    (see :func:`_extract_function_prefix_return_type`)."""
    name = _extract_subprogram_name(node, "FunctionStmt") or "anon_function"
    sub = IRSubprogram(
        name=_safe_name(name),
        display_name=name,
        kind="function",
        leading_comments=_subprogram_leading_comments(node, "FunctionStmt"),
        source=node.source,
    )

    # FunctionStmt structure: [PrefixSpec*, Name (function name), Name* (dummy args), Suffix?]
    # The dummy args are bare Name nodes, NOT wrapped in DummyArg like
    # in SubroutineStmt.
    dummy_arg_names = _extract_function_dummy_args(node)
    prefix_return_type = _extract_function_prefix_return_type(node)

    _lower_specification_and_execution(node, sub)
    _separate_parameters(sub, dummy_arg_names, node)
    _lift_function_return(sub, prefix_return_type, node)
    return sub


def _lower_block_data(node: Node) -> IRSubprogram | None:
    """Lower a Fortran ``BLOCK DATA`` unit to a synthetic init subroutine.

    ``BLOCK DATA`` is a special program unit whose entire purpose is to
    initialize the contents of ``COMMON`` blocks at program load time --
    no executable statements, just ``COMMON`` + ``DATA`` declarations.
    NRLMSISE-00 (IRI's MSIS atmosphere model) stuffs ~70 large coefficient
    tables into ``COMMON /PARM7/`` etc. here; without this initialization
    the C++ MSIS produces NaN temperatures and the IRI output table is
    junk.

    Lowering models it as a subroutine whose body is the DATA-derived
    assignments to its declared COMMON members.  State plumbing later
    threads each touched ``COMMON`` struct into it, and emission orders
    the main program to call it before the user body runs (see
    :func:`_emit_cpp_main`)."""
    name = (_extract_subprogram_name(node, "BlockDataStmt")
            or "block_data_anon")
    sub = IRSubprogram(
        name="block_data_init_" + _safe_name(name),
        display_name=name,
        kind="block_data",
        leading_comments=_subprogram_leading_comments(node, "BlockDataStmt"),
        source=node.source,
    )
    _lower_specification_and_execution(node, sub)
    _separate_parameters(sub, [], node)
    # A BLOCK DATA's COMMON members are not real locals -- they belong to
    # the shared struct.  ``state_plumbing`` will drop them and rewrite
    # references to the threaded ``<block>_common.field``.
    return sub


def _lower_subroutine(node: Node) -> IRSubprogram:
    """Lower a ``SUBROUTINE`` to an ``IRSubprogram`` of ``kind="subroutine"``.

    Pulls the dummy-arg names from the ``SubroutineStmt``, lowers the
    specification + execution parts, then separates parameters from
    locals.  Unlike :func:`_lower_function`, there's no return value to
    lift."""
    name = _extract_subprogram_name(node, "SubroutineStmt") or "anon_subroutine"
    sub = IRSubprogram(
        name=_safe_name(name),
        display_name=name,
        kind="subroutine",
        leading_comments=_subprogram_leading_comments(node, "SubroutineStmt"),
        source=node.source,
    )
    dummy_arg_names = _extract_subroutine_dummy_args(node)
    _lower_specification_and_execution(node, sub)
    _separate_parameters(sub, dummy_arg_names, node)
    return sub


# ---- Parameter / return-value plumbing ------------------------------------


def _extract_subroutine_dummy_args(subprog: Node) -> list[str]:
    """Pull the dummy arg names out of the leading ``SubroutineStmt``."""
    out: list[str] = []
    for stmt in subprog.children:
        if stmt.kind != "Statement":
            continue
        sub_stmt = stmt.find_first("SubroutineStmt")
        if sub_stmt is None:
            continue
        for arg in sub_stmt.children_of_kind("DummyArg"):
            name = arg.find_first("Name")
            if name is not None and name.fortran:
                out.append(_safe_name(name.fortran))
        return out
    return out


def _extract_function_dummy_args(subprog: Node) -> list[str]:
    """Pull dummy arg names from a ``FunctionStmt``.

    FunctionStmt's children look like ``[PrefixSpec*, Name (function),
    Name* (args), Suffix?]`` — all bare Names rather than DummyArgs.
    """
    out: list[str] = []
    for stmt in subprog.children:
        if stmt.kind != "Statement":
            continue
        func_stmt = stmt.find_first("FunctionStmt")
        if func_stmt is None:
            continue
        names = [c for c in func_stmt.children if c.kind == "Name"]
        # First Name is the function name; the rest are the dummy args.
        for name in names[1:]:
            if name.fortran:
                out.append(_safe_name(name.fortran))
        return out
    return out


def _extract_function_prefix_return_type(subprog: Node) -> IRType | None:
    """Read the function's result type straight from the resolved symbol
    flang attached to the function-defining ``Name``.

    ``Symbol::GetType()`` on a function returns the *result* type, so for
    ``real(8) function foo(x)`` the function-Name carries
    ``type="REAL(8)"`` (including the kind) — whether that came from a
    ``PrefixSpec`` (``real(8) function``), a separate `RESULT()` clause's
    declared variable, implicit typing, or anything else.  Reading the
    symbol means every result-type form is handled the same way."""
    for stmt in subprog.children:
        if stmt.kind != "Statement":
            continue
        func_stmt = stmt.find_first("FunctionStmt")
        if func_stmt is None:
            continue
        for child in func_stmt.children:
            if child.kind == "Name" and child.sym_type:
                ty = _scalar_type_from_fortran(child.sym_type)
                if ty is not None:
                    return ty
                break
        # Fallback: an explicit type prefix the symbol didn't preserve
        # (rare; covers the few cases where ``sym_type`` is missing).
        for prefix in func_stmt.find_all("PrefixSpec"):
            spec = prefix.find_first("DeclarationTypeSpec")
            if spec is not None:
                return lower_type_spec(spec)
    return None


def _separate_parameters(
    sub: IRSubprogram, arg_names: list[str], node: Node | None = None
) -> None:
    """Pull every local matching a dummy arg name out into ``parameters``."""
    if not arg_names:
        return
    wanted = {a.lower(): i for i, a in enumerate(arg_names)}
    # Dummy *procedure* arguments (a function/subroutine passed in and
    # invoked inside the routine).  flang marks these a procedure; a
    # ``real func`` / ``external func`` declaration only states the
    # result type, so the matching "local" is not a variable — it must
    # become a ``std::function`` parameter, not a scalar.
    proc_dummies = (
        _procedure_dummy_return_types(node, arg_names) if node is not None else {}
    )
    by_idx: list[tuple[int, IRParameter]] = []
    remaining: list[IRLocal] = []
    for loc in sub.locals:
        if loc.name in wanted:
            if loc.name in proc_dummies:
                continue  # placed below as a procedure parameter
            param = IRParameter(
                name=loc.name,
                type=loc.type,
                intent=loc.intent or "inout",
                intent_declared=loc.intent is not None,
                optional=loc.is_optional,
            )
            by_idx.append((wanted[loc.name], param))
        else:
            remaining.append(loc)
    # Synthesize the ``std::function`` parameter at its dummy position so
    # the signature's arity is correct and ``f(x)`` calls type-check —
    # whether or not the procedure also had an explicit result-type decl.
    for name, ret in proc_dummies.items():
        arity = _count_call_arity(sub.body, name)
        by_idx.append((wanted[name], _procedure_param(name, ret, arity)))
    by_idx.sort(key=lambda t: t[0])
    sub.parameters = [p for _, p in by_idx]
    # Drop scalar locals that are really EXTERNAL-procedure declarations
    # (a ``real f`` result-type decl shadowing the procedure ``f``).  Keep
    # the function's own result variable (shares the unit's name), the
    # unit's ENTRY result variables (also ``is_proc`` to flang, but they
    # are result storage shared across entries, not externals to call), and
    # statement functions (lowered here to a lambda local that must stay).
    if node is not None:
        externals = (
            _external_procedure_names(node) - {sub.name} - _entry_names(node)
        )
        sibling_proc_dummies = _entry_dummy_arg_names(node) - set(wanted)
        kept: list[IRLocal] = []
        for loc in remaining:
            if loc.name not in externals or isinstance(loc.initializer, IRLambda):
                kept.append(loc)
                continue
            # A dummy *procedure* of a sibling ENTRY that this function does
            # not receive as a parameter is reached only in code guarded by
            # an earlier RETURN.  Re-type its result decl as an empty
            # ``std::function`` local so the (dead) call type-checks instead
            # of naming an undeclared symbol.  A plain EXTERNAL called
            # directly (not an entry dummy) is still dropped so the call
            # resolves to the real global.
            if loc.name in sibling_proc_dummies and _is_called(
                sub.body, loc.name
            ):
                arity = _count_call_arity(sub.body, loc.name)
                loc.type = _procedure_param(loc.name, loc.type, arity).type
                kept.append(loc)
        remaining = kept
    sub.locals = remaining
    _deref_optional_params(sub)


def _procedure_param(name: str, ret: IRType | None, arity: int) -> IRParameter:
    """A ``std::function``-typed parameter for a dummy procedure.

    ``ret is None`` means a dummy *subroutine* (``void`` result).  The
    argument types are unknown under FORTRAN 77's implicit interface;
    ``float`` covers the numeric procedures these are in practice, and
    other numeric actuals convert implicitly at the (lambda) call site."""
    ret_cpp = ret.cpp if ret is not None else "void"
    args = ", ".join(["float"] * max(arity, 0))
    return IRParameter(
        name=name,
        type=IRType(
            cpp=f"std::function<{ret_cpp}({args})>",
            fortran="procedure",
            is_procedure=True,
            proc_arity=arity,
        ),
        intent="in",
    )


def _procedure_dummy_return_types(
    node: Node, arg_names: list[str]
) -> dict[str, IRType | None]:
    """Dummy arg names flang resolved as procedures, mapped to their
    result type (``None`` for a subroutine).  Restricted to *this* unit's
    names so a like-named object in another routine isn't misread."""
    wanted = set(arg_names)
    out: dict[str, IRType | None] = {}
    for n in _unit_names(node):
        if not n.fortran:
            continue
        key = _safe_name(n.fortran)
        if key not in wanted or key in out:
            continue
        if getattr(n, "is_proc", False) and not n.is_object:
            out[key] = _scalar_type_from_fortran(n.sym_type) if n.sym_type else None
    return out


def _external_procedure_names(node: Node) -> set[str]:
    """Names flang resolves as procedures (EXTERNAL functions/subroutines)
    in this unit.  An ``external f`` / ``real f`` pair declares ``f``'s
    result type, not a variable; the resulting scalar local would shadow
    the real procedure, so callers (or a dummy-procedure lambda wrapping
    it) can't invoke it.  Such locals must be dropped."""
    out: set[str] = set()
    for n in _unit_names(node):
        if not n.fortran:
            continue
        if getattr(n, "is_proc", False) and not n.is_object:
            out.add(_safe_name(n.fortran))
    return out


def _entry_names(node: Node) -> set[str]:
    """The names introduced by this unit's ``ENTRY`` statements."""
    out: set[str] = set()
    for st in _unit_descendants(node, "EntryStmt"):
        nm = st.first_child("Name")
        if nm is not None and nm.fortran:
            out.add(_safe_name(nm.fortran))
    return out


def _entry_dummy_arg_names(node: Node) -> set[str]:
    """Every dummy-argument name across this unit's ``ENTRY`` statements.

    Used to tell a sibling entry's dummy procedure (which a function not
    listing it must still declare, for the RETURN-guarded dead call) apart
    from a plain EXTERNAL that's called directly and should resolve to the
    real global."""
    out: set[str] = set()
    for st in _unit_descendants(node, "EntryStmt"):
        for da in st.children:
            if da.kind == "DummyArg":
                nm = da.first_child("Name")
                if nm is not None and nm.fortran:
                    out.add(_safe_name(nm.fortran))
    return out


def _is_called(body: list[IRStatement], name: str) -> bool:
    """True if ``name`` is invoked as a function call or ``CALL`` statement
    anywhere in ``body`` (unlike :func:`_count_call_arity`, no fallback)."""
    hit = [False]

    def see_expr(e: IRExpr) -> IRExpr:
        if isinstance(e, IRFunctionCall) and e.callee == name:
            hit[0] = True
        return e

    def see_stmt(s: IRStatement) -> IRStatement:
        if isinstance(s, IRCall) and s.callee == name:
            hit[0] = True
        return s

    for s in body:
        map_statement(s, on_stmt=see_stmt, on_expr=lambda e: map_expr(e, see_expr))
    return hit[0]


def _count_call_arity(body: list[IRStatement], name: str) -> int:
    """Largest argument count among calls to ``name`` in ``body`` (both
    function-call expressions and subroutine-call statements).

    Returns the true maximum — which may be 0 for a dummy invoked only as
    ``f()`` / ``CALL f`` — so a zero-argument procedure dummy isn't given a
    spurious 1-argument signature.  Falls back to 1 only when ``name`` is
    never called locally (and no actual procedure pins down its arity)."""
    best = -1  # -1 = never seen called

    def see_expr(e: IRExpr) -> IRExpr:
        nonlocal best
        if isinstance(e, IRFunctionCall) and e.callee == name:
            best = max(best, len(e.args))
        return e

    def see_stmt(s: IRStatement) -> IRStatement:
        nonlocal best
        if isinstance(s, IRCall) and s.callee == name:
            best = max(best, len(s.args))
        return s

    for s in body:
        map_statement(s, on_stmt=see_stmt, on_expr=lambda e: map_expr(e, see_expr))
    return best if best >= 0 else 1


def _deref_optional_params(sub: IRSubprogram) -> None:
    """Rewrite value uses of an OPTIONAL scalar parameter ``p`` to
    ``p.value()`` (it's a std::optional in C++).  ``present(p)`` was
    already lowered to ``p.has_value()`` as raw text, so it is not a
    bare IRName and is left untouched."""
    opt_names = {
        p.name for p in sub.parameters if p.optional and not p.type.is_array
    }
    if not opt_names:
        return

    def deref(e: IRExpr) -> IRExpr:
        if isinstance(e, IRName) and e.name in opt_names:
            return IRRaw(f"{e.name}.value()")
        return e

    sub.body = [
        map_statement(s, on_expr=lambda e: map_expr(e, deref)) for s in sub.body
    ]


def _implicit_scalar_type(name: str) -> IRType:
    """The default FORTRAN 77 implicit type for ``name``: ``integer`` for
    initials I-N, otherwise ``real``."""
    first = name[0].lower() if name else "x"
    if "i" <= first <= "n":
        return IRType(cpp="int32_t", fortran="integer", is_integer=True)
    return IRType(cpp="float", fortran="real", is_real=True)


def _lift_function_return(
    sub: IRSubprogram, prefix_type: IRType | None, node: Node
) -> None:
    """Turn the local variable named after the function into a return
    value.  Renames every reference to ``<name>`` in the body to
    ``<name>_result`` so the emitter can finish with ``return
    <name>_result;``.
    """
    if sub.kind != "function":
        return

    # Find the local whose name matches the function name, if any.
    func_local = None
    for i, loc in enumerate(sub.locals):
        if loc.name == sub.name:
            func_local = loc
            sub.locals.pop(i)
            break

    return_type = (
        func_local.type if func_local is not None else prefix_type
    )
    if return_type is None:
        # No explicit declaration: use flang's resolved type for the
        # function-result symbol, then the F77 first-letter implicit rule.
        # An ``auto`` return would be ill-formed once the function is
        # forward-declared and called (which the prototype pass does).
        return_type = _resolved_types(node).get(sub.name) or _implicit_scalar_type(
            sub.display_name
        )

    # An assumed-length ``CHARACTER*(*)`` result is typed as a non-owning
    # ``std::string_view`` (the same spelling as an assumed-length dummy).
    # As a *function result* that view has no backing storage, so the
    # function would return an empty / dangling string.  Promote it to an
    # owning ``ftn::DynString`` returned by value: the callee carries its
    # own storage and the caller assigns the value into its own slot.
    if (
        return_type is not None
        and return_type.is_character
        and return_type.cpp == "std::string_view"
    ):
        return_type = dataclasses.replace(return_type, cpp="ftn::DynString")
    sub.return_type = return_type

    result_name = sub.name + "_result"
    sub.locals.insert(
        0,
        IRLocal(name=result_name, type=return_type),
    )

    def fix_return(stmt: IRStatement) -> IRStatement:
        # A bare ``RETURN`` inside a function exits with the result
        # variable's current value; emit ``return <name>_result;``.
        if isinstance(stmt, IRReturn) and stmt.value is None:
            return IRReturn(
                value=IRName(name=result_name, fortran=result_name),
                leading_comments=stmt.leading_comments,
                trailing_comments=stmt.trailing_comments,
            )
        return stmt

    sub.body = [
        map_statement(
            s,
            on_expr=lambda e: rename_var(e, sub.name, result_name),
            on_stmt=fix_return,
        )
        for s in sub.body
    ]


def _extract_subprogram_name(node: Node, header_kind: str) -> str | None:
    """Pull the declared subprogram name out of its header statement."""
    for stmt in node.children:
        if stmt.kind != "Statement":
            continue
        for inner in stmt.walk():
            if inner.kind == header_kind:
                # The name is the first *direct* Name child.  A type or
                # attribute prefix (e.g. ``real(kind=rp) function foo``)
                # contains its own Name nodes — the kind — so a deep walk
                # would wrongly return ``rp``; iterate direct children only.
                for child in inner.children:
                    if child.kind == "Name" and child.fortran:
                        return child.fortran
                return None
    return None


def _lower_specification_and_execution(node: Node, sub: IRSubprogram) -> None:
    """Walk the SpecificationPart (declarations) and ExecutionPart (body)."""
    global _FORMAT_LABELS
    _FORMAT_LABELS = _build_format_labels(node)
    spec_part: Node | None = None
    for child in node.children:
        if child.kind == "SpecificationPart":
            spec_part = child
            sub.locals.extend(_lower_specification(child))
            sub.common_uses.extend(_lower_common_statements(child))
            sub.equiv_groups.extend(
                _lower_equivalence_statements(child, sub)
            )
            # Drop the equiv-aliased locals — they re-appear as typed
            # proxies on each equiv struct, bound back to their original
            # names at the top of the body so call sites stay readable.
            _drop_equiv_aliased_locals(sub)
            sub.used_modules.extend(_lower_use_statements(child))
        elif child.kind == "ExecutionPart":
            sub.body.extend(_lower_execution(child))
    # Prefer flang's resolved types (handles KINDs, ``integer*8``, custom
    # IMPLICIT, etc.) over the parse-tree spelling for scalar locals, and
    # for the implicit-typing synthesis below.
    resolved = _resolved_types(node)
    array_resolved = _resolved_array_types(node)
    for loc in sub.locals:
        if loc.type.is_pointer:
            continue
        if isinstance(loc.initializer, IRLambda):
            continue  # statement function: keep the deduced ``auto`` type
        if not loc.type.is_array:
            # A scalar parse-tree spelling that flang resolved as an array
            # — e.g. the dimension is on the COMMON statement
            # (``common /x/ a(81,5)``) rather than a DIMENSION/type decl.
            at = array_resolved.get(loc.name)
            if at is not None:
                loc.type = at
                continue
            rt = resolved.get(loc.name)
            if rt is not None and not rt.is_array:
                loc.type = rt
    # FORTRAN 77 implicit typing: synthesize declarations for undeclared
    # variables (must precede array-assignment expansion, which keys off
    # which locals are arrays).
    _apply_implicit_typing(node, sub, resolved)
    # Drop again now that implicit typing has run: it would re-add an
    # equiv-aliased name because it sees the body referencing a "missing"
    # local.  The equiv struct already provides storage; the alias is a
    # binding, not a separate declaration.
    _drop_equiv_aliased_locals(sub)
    # DATA initializations run after implicit typing so array-vs-scalar is
    # known, and before the executable body.  Collected unit-wide because
    # F77 allows DATA among executable statements, not just declarations.
    data_inits = _lower_data_statements(node, sub.locals)
    sub.body = data_inits + sub.body
    # A bare ``SAVE`` (no entity list) saves every eligible local.  flang
    # records this as a subprogram-level fact rather than a per-symbol attr,
    # so it never reaches ``is_save`` via ``name.attrs``; detect the bare
    # statement here and mark the locals.  Essential for the umbrella-with-
    # ENTRY idiom, where the shared state is invariably a bare ``SAVE``.
    _apply_bare_save(node, sub)
    # A DATA initializer of a SAVEd scalar runs once at load time, not on
    # every call.  It is lowered as an assignment at the top of the body;
    # left there it re-initializes the SAVE state on each call -- and once
    # the routine has ENTRY points, that reset lands in *every* entry (so
    # e.g. CHKOUT zeroes the trace-stack depth before reading it).  Hoist it
    # to the local's initializer (which becomes the SAVE-struct field's
    # once-only initializer) and drop it from the per-call body.
    _hoist_save_data_inits(sub, data_inits)
    # A SAVEd *array's* DATA initializer is likewise load-once, but can't be
    # folded into a struct-field initializer; left in the body it re-runs
    # every call, wiping state that a first-call block set up (NPARSD zeroes
    # its CLASS table each call, so the second parse sees no digits).  Guard
    # those inits with a once-only SAVE flag.
    _guard_save_array_data_inits(sub, data_inits)
    _resolve_allocations(sub)
    _resolve_pointers(sub)
    _expand_array_assignments(sub, array_resolved)
    # ENTRY statements split this unit into several alternate entry points
    # that share its storage.  Carve each one out (statements from the
    # entry onward) as its own subprogram before goto-structuring, which
    # rewrites the body irreversibly.
    _split_entry_points(sub, node, data_inits)
    # ``READ(..., END=label, ERR=label, IOSTAT=v)`` -- expand each labeled
    # status spec into a synthetic ``if (!stream) goto label;`` right
    # after the read, so the goto-structuring pass below converts it
    # uniformly along with every other goto.  Done at this point because
    # the structurer is what turns goto into ``_pc`` state transitions.
    _expand_io_label_jumps(sub.body)
    # Eliminate goto in favor of structured control flow.
    sub.body, used_dispatch = structure_gotos(sub.body)
    # ``structure_gotos`` only reports the *top-level* dispatch; nested
    # dispatches use distinct state variables ``_pc1``, ``_pc2``, etc.
    # Scan the structured body for every ``_pc*`` name actually emitted
    # and declare a local for each.
    pc_names = _pc_names_used(sub.body)
    if used_dispatch:
        pc_names.add("_pc")
    for name in sorted(pc_names):
        sub.locals.append(
            IRLocal(
                name=name,
                type=IRType(cpp="int", fortran="integer", is_integer=True),
            )
        )


def _expand_io_label_jumps(body: list[IRStatement]) -> None:
    """Rewrite each ``IRRead`` carrying ``end_label``/``err_label`` into a
    plain ``IRRead`` followed by a synthetic conditional ``IRGoto`` that
    jumps when the stream signals end-of-file (or, for ERR=, any non-EOF
    failure).  Also assigns ``IOSTAT=`` after the read.  Operates in
    place, recursively into every nested child body."""
    from .structure import _child_bodies  # local: structure imports from lowering too

    i = 0
    while i < len(body):
        stmt = body[i]
        for child in _child_bodies(stmt):
            _expand_io_label_jumps(child)
        if isinstance(stmt, IRRead) and (
            stmt.end_label is not None
            or stmt.err_label is not None
            or stmt.iostat_target is not None
        ):
            if stmt.internal_unit is not None:
                stream = _render_expr_inline(stmt.internal_unit)
            elif stmt.whole_line and stmt.unit_text is not None:
                # A whole-line read consumes from the *unfiltered* stream, so
                # its EOF / error / IOSTAT state lives there, not on ``in()``.
                stream = f"_units.in_raw({stmt.unit_text})"
            else:
                stream = stmt.stream
            # Replace the IRRead with one that no longer carries the
            # status spec (so emit doesn't try to handle it).  Preserve
            # every other field so a labeled read still picks the
            # fixed-width emit path when ``fields`` is set.
            cleaned = IRRead(
                items=stmt.items,
                stream=stmt.stream,
                internal_unit=stmt.internal_unit,
                unit_text=stmt.unit_text,
                fields=stmt.fields,
                whole_line=stmt.whole_line,
                leading_comments=stmt.leading_comments,
                trailing_comments=stmt.trailing_comments,
            )
            inserts: list[IRStatement] = [cleaned]
            if stmt.iostat_target is not None:
                # iostat: 0 on success, -1 on EOF, 1 on other failure.
                inserts.append(
                    IRAssignment(
                        target=stmt.iostat_target,
                        value=IRRaw(
                            f"({stream}.fail() ? "
                            f"({stream}.eof() ? -1 : 1) : 0)"
                        ),
                    )
                )
            if stmt.end_label is not None and stmt.err_label is not None:
                # Two distinct labels: EOF goes to end, non-EOF failure to err.
                inserts.append(
                    IRGoto(
                        target=stmt.end_label,
                        condition=IRRaw(f"({stream}.eof())"),
                    )
                )
                inserts.append(
                    IRGoto(
                        target=stmt.err_label,
                        condition=IRRaw(
                            f"({stream}.fail() && !{stream}.eof())"
                        ),
                    )
                )
            elif stmt.end_label is not None:
                inserts.append(
                    IRGoto(
                        target=stmt.end_label,
                        condition=IRRaw(f"(!{stream})"),
                    )
                )
            elif stmt.err_label is not None:
                inserts.append(
                    IRGoto(
                        target=stmt.err_label,
                        condition=IRRaw(f"(!{stream})"),
                    )
                )
            body[i : i + 1] = inserts
            i += len(inserts)
            continue
        i += 1


def _apply_bare_save(node: Node, sub: IRSubprogram) -> None:
    """A bare ``SAVE`` statement (no entity list) gives the SAVE attribute
    to every eligible local of the unit.  flang carries this as a
    subprogram-level fact, not a per-symbol attr, so ``is_save`` (read from
    ``name.attrs``) never sees it.  Detect the childless ``SaveStmt`` and
    mark the locals here.

    Excluded: PARAMETER constants (already ``static constexpr``), COMMON
    members (persisted via their block's struct), and statement functions
    (lowered to lambdas, not storage)."""
    bare = any(
        not st.children for st in _unit_descendants(node, "SaveStmt")
    )
    if not bare:
        return
    # Dummy arguments are never SAVEd (a SAVE of a dummy is illegal); a name
    # that is a dummy of this unit or any of its ENTRY points may still sit
    # in ``sub.locals`` (an entry's dummy is the primary's local), so exclude
    # the whole dummy set.  Read the primary's dummies from the parse tree:
    # parameter separation runs *after* this point, so ``sub.parameters`` is
    # still empty and the dummies are sitting in ``sub.locals``.
    dummies = (
        set(_extract_subroutine_dummy_args(node))
        | set(_extract_function_dummy_args(node))
        | _entry_dummy_arg_names(node)
    )
    for loc in sub.locals:
        if (
            loc.is_parameter
            or loc.common_block
            or loc.name in dummies
            or isinstance(loc.initializer, IRLambda)
        ):
            continue
        loc.is_save = True


def _hoist_save_data_inits(
    sub: IRSubprogram, data_inits: list[IRStatement]
) -> None:
    """Move a SAVEd *scalar* local's DATA initializer from the body to the
    local's ``initializer`` (so it becomes the once-only SAVE-struct field
    initializer) and remove the now-redundant body assignment.

    DATA init of a SAVEd variable is a load-time, run-once initialization;
    leaving it as a body statement re-runs it on every call, corrupting the
    persisted state -- and after ENTRY splitting it would be copied into
    every entry's prologue.  Only plain ``name = <constant>`` inits of
    non-array SAVE locals are hoisted (the umbrella-state case); array DATA
    inits are left alone."""
    save_scalars = {
        loc.name: loc
        for loc in sub.locals
        if loc.is_save and not loc.type.is_array
    }
    if not save_scalars:
        return

    def _is_pure_literal(expr: IRExpr) -> bool:
        # A struct-scope field initializer can't see function-scope names
        # (PARAMETER constexprs, other locals).  Hoist only values built
        # purely from literals/operators so ``DATA STHEAD /NIL/`` (NIL a
        # PARAMETER) stays a body assignment where NIL is in scope.
        if isinstance(expr, IRLiteral):
            return True
        if isinstance(expr, IRUnaryOp):
            return _is_pure_literal(expr.operand)
        if isinstance(expr, IRBinaryOp):
            return _is_pure_literal(expr.lhs) and _is_pure_literal(expr.rhs)
        if isinstance(expr, IRCast):
            return _is_pure_literal(expr.operand)
        return False

    hoisted_stmts: list[IRStatement] = []
    for stmt in data_inits:
        if (
            isinstance(stmt, IRAssignment)
            and isinstance(stmt.target, IRName)
            and stmt.target.name in save_scalars
            and _is_pure_literal(stmt.value)
        ):
            loc = save_scalars[stmt.target.name]
            if loc.initializer is None:
                loc.initializer = stmt.value
                hoisted_stmts.append(stmt)
    if not hoisted_stmts:
        return
    drop = set(map(id, hoisted_stmts))
    data_inits[:] = [s for s in data_inits if id(s) not in drop]
    sub.body = [s for s in sub.body if id(s) not in drop]


def _guard_save_array_data_inits(
    sub: IRSubprogram, data_inits: list[IRStatement]
) -> None:
    """Wrap the DATA initializers of SAVEd *arrays* in a once-only guard so
    they run at first call (load-time semantics) instead of every call.

    A SAVEd array's DATA init is an assignment at the top of the body; unlike
    a scalar it can't become a struct-field initializer, so without a guard
    it re-initializes the persisted array on each call -- clobbering any
    state a first-call (``IF (FIRST)``) block established (the SPICE NPARSD
    CLASS/VALUES tables).  A synthetic SAVEd flag makes the block run once."""
    # Every SAVE local whose DATA init is *still* in the body: the pure-
    # literal scalars were already folded into struct-field initializers by
    # _hoist_save_data_inits, so what remains is SAVE arrays plus SAVE
    # scalars whose value references a PARAMETER (``DATA SAVACT / IDEFLT /``)
    # -- those can't be struct-field initializers (the constant isn't in
    # struct scope), but they must still run once, or every call resets the
    # persisted state (GETACT kept returning the default error action, so
    # ERRACT('SET','RETURN') never stuck and every SPICE error aborted).
    save_targets = {loc.name for loc in sub.locals if loc.is_save}
    if not save_targets:
        return
    guarded = [
        s
        for s in data_inits
        if isinstance(s, IRAssignment)
        and isinstance(s.target, IRName)
        and s.target.name in save_targets
    ]
    if not guarded:
        return
    flag = "_save_data_init"
    if not any(loc.name == flag for loc in sub.locals):
        sub.locals.append(
            IRLocal(
                name=flag,
                type=IRType(cpp="bool", fortran="logical", is_logical=True),
                is_save=True,
                initializer=IRLiteral(cpp_text="false", cpp_type="bool"),
            )
        )
    drop = set(map(id, guarded))
    block: list[IRStatement] = list(guarded) + [
        IRAssignment(
            target=IRName(name=flag, fortran=flag),
            value=IRLiteral(cpp_text="true", cpp_type="bool"),
        )
    ]
    guard = IRIf(
        branches=[
            (IRUnaryOp(op="!", operand=IRName(name=flag, fortran=flag)), block)
        ],
        else_body=None,
    )
    # Replace the first guarded init (in body and data_inits) with the guard
    # block; drop the rest.  Keeps the inits' original position at body top.
    def splice(stmts: list[IRStatement]) -> list[IRStatement]:
        out: list[IRStatement] = []
        placed = False
        for s in stmts:
            if id(s) in drop:
                if not placed:
                    out.append(guard)
                    placed = True
            else:
                out.append(s)
        return out

    data_inits[:] = splice(data_inits)
    sub.body = splice(sub.body)


def _split_entry_points(
    sub: IRSubprogram, node: Node, data_inits: list[IRStatement]
) -> None:
    """Turn each ``ENTRY`` marker in ``sub.body`` into a standalone
    subprogram on ``sub.entry_points``, and strip the markers from the
    primary body.

    An alternate entry shares the unit's storage and starts at its own
    statement; the equivalent standalone routine runs the tail of the body
    from that point (plus the unit's DATA initializers, which every entry
    would have applied at load time).  Each entry carries its own dummy
    arguments; the rest of the unit's variables remain ordinary locals."""
    positions = [
        i for i, st in enumerate(sub.body) if isinstance(st, IREntry)
    ]
    if not positions:
        return
    # The primary and every alternate entry form one group that shares a
    # single SAVE state struct (see IRSubprogram.entry_group).
    sub.entry_group = sub.name
    markers = [sub.body[i] for i in positions]
    # In a multi-entry FUNCTION every entry (and the primary) has its own
    # result variable named after it, and they share storage.  Each result
    # name therefore appears as an assignable variable throughout the unit's
    # body, including the parts that belong to other entries.  Declare every
    # result name as a local in each generated function so an assignment to
    # a *sibling* entry's name (``ZZUNPCK = .TRUE.`` reached from the
    # primary) isn't mistaken for an assignment to the global function;
    # each function still lifts its *own* name into the return value.
    resolved = _resolved_types(node) if sub.kind == "function" else {}
    result_names = (
        {sub.name} | {m.name for m in markers} if sub.kind == "function" else set()
    )

    def add_result_locals(target: IRSubprogram, own: str) -> None:
        present = {loc.name for loc in target.locals} | {
            p.name for p in target.parameters
        }
        for rn in result_names:
            if rn == own or rn in present:
                continue
            ty = resolved.get(rn) or _implicit_scalar_type(rn)
            target.locals.insert(0, IRLocal(name=rn, type=ty))

    for idx, marker in zip(positions, markers):
        tail = [st for st in sub.body[idx + 1 :] if not isinstance(st, IREntry)]
        entry = IRSubprogram(
            name=marker.name,
            display_name=marker.name,
            kind=sub.kind,
            locals=copy.deepcopy(sub.locals),
            body=copy.deepcopy(data_inits) + copy.deepcopy(tail),
            source=sub.source,
            common_uses=list(sub.common_uses),
            used_modules=list(sub.used_modules),
            equiv_groups=copy.deepcopy(sub.equiv_groups),
            parent_module=sub.parent_module,
        )
        entry.body, used_dispatch = structure_gotos(entry.body)
        pc_names = _pc_names_used(entry.body)
        if used_dispatch:
            pc_names.add("_pc")
        for name in sorted(pc_names):
            entry.locals.append(
                IRLocal(
                    name=name,
                    type=IRType(cpp="int", fortran="integer", is_integer=True),
                )
            )
        _separate_parameters(entry, list(marker.arg_names), node)
        if sub.kind == "function":
            add_result_locals(entry, marker.name)
            _lift_function_return(entry, None, node)
        entry.entry_group = sub.name
        sub.entry_points.append(entry)
    # The primary normally keeps the whole body (entries' code is reachable
    # by fall-through / GOTO into shared code, e.g. FELDG).  But when the
    # primary's own section ends in an unconditional RETURN right before the
    # first ENTRY, the following entry code is *unreachable* from the
    # primary and belongs only to the entries (the ENCHAR/DECHAR pattern);
    # keeping it would, e.g., make a parameter the primary only reads look
    # written (a dead ``number = ...`` in DECHAR's code).  Drop it there.
    first = positions[0]
    head = [s for s in sub.body[:first] if not isinstance(s, IRComment)]
    if head and isinstance(head[-1], IRReturn):
        sub.body = sub.body[:first]
    else:
        sub.body = [st for st in sub.body if not isinstance(st, IREntry)]
    add_result_locals(sub, sub.name)


def _references_name(body: list[IRStatement], name: str) -> bool:
    found = [False]

    def note(expr: IRExpr) -> IRExpr:
        if isinstance(expr, IRName) and expr.name == name:
            found[0] = True
        return expr

    for stmt in body:
        map_statement(stmt, on_expr=lambda e: map_expr(e, note))
    return found[0]


_PC_NAME_RE = re.compile(r"^_pc\d*$")


def _pc_names_used(body: list[IRStatement]) -> set[str]:
    """Collect every ``_pc``/``_pcN`` dispatch state variable referenced
    in ``body``.  Nested dispatch loops use distinct state variables so
    each one needs its own local declaration."""
    names: set[str] = set()

    def note(expr: IRExpr) -> IRExpr:
        if isinstance(expr, IRName) and _PC_NAME_RE.match(expr.name):
            names.add(expr.name)
        return expr

    for stmt in body:
        map_statement(stmt, on_expr=lambda e: map_expr(e, note))
    return names


_FTYPE_RE = re.compile(r"^\s*([A-Za-z ]+?)\s*(?:\(([^)]*)\))?\s*$")


def _scalar_type_from_fortran(spelling: str) -> IRType | None:
    """Map a resolved type spelling from the dumper (e.g. ``"REAL(8)"``,
    ``"INTEGER(4)"``, ``"TYPE(point)"``) to an :class:`IRType` (element
    type for arrays).  Returns ``None`` for spellings we don't model."""
    m = _FTYPE_RE.match(spelling)
    if m is None:
        return None
    cat = m.group(1).strip().upper()
    arg = (m.group(2) or "").strip()

    def _first_int(text: str) -> int | None:
        mm = re.match(r"\s*(\d+)", text)
        return int(mm.group(1)) if mm is not None else None

    if cat == "INTEGER":
        return IRType(cpp=_INT_KIND_CPP.get(_first_int(arg), "int32_t"),
                      fortran=spelling, is_integer=True)
    if cat in ("REAL", "DOUBLE PRECISION"):
        kind = 8 if cat == "DOUBLE PRECISION" else _first_int(arg)
        cpp = {None: "float", 4: "float", 8: "double", 16: "long double"}.get(
            kind, "float"
        )
        return IRType(cpp=cpp, fortran=spelling, is_real=True)
    if cat == "LOGICAL":
        return IRType(cpp="bool", fortran=spelling, is_logical=True)
    if cat == "COMPLEX":
        inner = {None: "float", 4: "float", 8: "double"}.get(_first_int(arg), "float")
        return IRType(cpp=f"std::complex<{inner}>", fortran=spelling)
    if cat in ("TYPE", "CLASS"):
        name = arg.split(",")[0].strip()
        return IRType(cpp=camelcase(name), fortran=spelling) if name else None
    if cat == "CHARACTER":
        # flang spells the length as ``CHARACTER(14_8,1)`` — the first
        # selector is the length, possibly kind-suffixed (``14_8``).  An
        # assumed-length dummy is spelled ``CHARACTER(*,1)``.
        first = arg.split(",")[0].strip() if arg else ""
        if first == "*":
            return IRType(
                cpp="std::string_view",
                fortran=spelling,
                is_character=True,
            )
        length = _first_int(first)
        if length is not None:
            return IRType(
                cpp=f"ftn::FortranString<{length}>",
                fortran=spelling,
                is_character=True,
            )
        return None
    return None


def _unit_descendants(node: Node, kind: str) -> Iterable[Node]:
    """Yield descendants of ``node`` of the given kind, *not* descending
    into nested (CONTAINS) subprograms — they belong to those units."""
    for child in node.children:
        if child.kind in ("InternalSubprogramPart", "ModuleSubprogramPart"):
            continue
        if child.kind == kind:
            yield child
        yield from _unit_descendants(child, kind)


def _unit_names(node: Node) -> Iterable[Node]:
    """Yield the Name nodes belonging to ``node`` itself (see
    _unit_descendants)."""
    return _unit_descendants(node, "Name")


def _resolved_types(node: Node) -> dict[str, IRType]:
    """Collect resolved scalar/element types for every Name in ``node``
    that the dumper annotated, keyed by the safe-named identifier."""
    out: dict[str, IRType] = {}
    for n in _unit_names(node):
        if n.sym_type and n.fortran:
            key = _safe_name(n.fortran)
            if key not in out:
                ty = _scalar_type_from_fortran(n.sym_type)
                if ty is not None:
                    out[key] = ty
    return out


def _resolved_array_types(node: Node) -> dict[str, IRType]:
    """Full ``ftn::Array`` IRTypes for names flang resolved with a
    constant array shape, keyed by safe name.  Lets a scalar-looking
    declaration whose dimension lives on the COMMON statement
    (``common /x/ a(81,5)``) be typed as an array."""
    elem: dict[str, IRType] = {}
    shapes: dict[str, list[tuple[int, int]]] = {}
    ranks: dict[str, int] = {}
    for n in _unit_names(node):
        if not n.fortran:
            continue
        key = _safe_name(n.fortran)
        if n.sym_type and key not in elem:
            ty = _scalar_type_from_fortran(n.sym_type)
            if ty is not None:
                elem[key] = ty
        if n.rank and key not in ranks:
            ranks[key] = n.rank
        if n.shape and key not in shapes:
            shapes[key] = n.shape
    out: dict[str, IRType] = {}
    for key, element in elem.items():
        rank = ranks.get(key, 0)
        if rank > 0 and key in shapes:
            out[key] = _array_type_from_shape(element, shapes[key])
        elif rank > 0:
            out[key] = _deferred_array_type(element, rank)
    return out


def _apply_implicit_typing(
    node: Node, sub: IRSubprogram, resolved: dict[str, IRType]
) -> None:
    """Declare variables that have no explicit declaration.

    Driven by facts from flang's symbol table (emitted on each Name): a
    name is a local of this unit when it is an object entity, is not a
    procedure, and is not module/host-associated.  Type and array shape
    both come from the resolved symbol (emitted on each Name); there is no
    fallback guess — a name flang did not type is left undeclared.
    """
    # An implicitly-typed dummy argument (no explicit declaration — common
    # in FORTRAN 77) has no local to ``_separate_parameters`` into the
    # signature, so it must be synthesized here just like any other
    # undeclared local; it is moved into ``parameters`` afterwards.  Only
    # *declared* names (already in ``sub.locals``) are excluded.
    known = {loc.name for loc in sub.locals}
    known.add(sub.name)
    known.add(sub.name + "_result")
    # Statement-function names and their dummy args are not unit locals.
    for sf in _unit_descendants(node, "StmtFunctionStmt"):
        for nm in (c for c in sf.children if c.kind == "Name" and c.fortran):
            known.add(_safe_name(nm.fortran))

    # Local object-entity variables of this unit, from symbol facts: their
    # resolved type and (constant) array shape.
    types: dict[str, IRType] = {}
    shapes: dict[str, list[tuple[int, int]]] = {}
    ranks: dict[str, int] = {}
    candidates: list[str] = []
    seen: set[str] = set()
    for n in _unit_names(node):
        if not n.fortran:
            continue
        if not n.is_object or n.is_proc or n.assoc is not None:
            continue
        key = _safe_name(n.fortran)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(key)
        if n.rank:
            ranks[key] = n.rank
        if n.shape:
            shapes[key] = n.shape
        ty = resolved.get(key)
        if ty is not None:
            types[key] = ty

    for nm in sorted(candidates):
        if nm in known:
            continue
        elem = types.get(nm)
        if elem is None:
            # No resolved type — nothing to declare from (do not guess).
            continue
        rank = ranks.get(nm, 0)
        if rank > 0 and nm in shapes:
            ty = _array_type_from_shape(elem, shapes[nm])
        elif rank > 0:
            # Rank known but shape not constant (an adjustable-bound dummy
            # array, ``a(n)``); the rank alone is enough — as a dummy it
            # becomes a non-owning ArrayRef sized by the caller.
            ty = _deferred_array_type(elem, rank)
        else:
            ty = elem
        sub.locals.append(IRLocal(name=nm, type=ty))


def _deferred_array_type(element_type: IRType, rank: int) -> IRType:
    """A rank-``rank`` array with unknown extents (an adjustable/assumed
    dummy array, or a deferred-shape local).  As a dummy this lowers to a
    non-owning ``ArrayRef<T, rank>``; as a local it default-constructs."""
    return IRType(
        cpp=f"ftn::Array<{element_type.cpp}, {rank}>",
        fortran=f"{element_type.fortran}, dimension({rank})",
        is_array=True,
        array_rank=rank,
        array_extent_exprs=(),
        array_static=False,
        element_type_cpp=element_type.cpp,
        is_integer=element_type.is_integer,
        is_real=element_type.is_real,
        is_logical=element_type.is_logical,
        is_character=element_type.is_character,
    )


def _array_type_from_shape(
    element_type: IRType, dims: list[tuple[int, int]]
) -> IRType:
    """Build a ``ftn::Array<T, Rank>`` IRType from a resolved symbol's
    constant shape (per-dimension inclusive ``(lower, upper)`` bounds)."""
    lowers = [lo for lo, _ in dims]
    extents = [hi - lo + 1 for lo, hi in dims]
    rank = len(dims)
    has_explicit_lower = any(lo != 1 for lo in lowers)
    return IRType(
        cpp=f"ftn::Array<{element_type.cpp}, {rank}>",
        fortran=f"{element_type.fortran}, dimension({rank})",
        is_array=True,
        array_rank=rank,
        array_extent_exprs=tuple(str(e) for e in extents),
        array_lower_bound_exprs=(
            tuple(str(lo) for lo in lowers) if has_explicit_lower else ()
        ),
        array_static=True,
        element_type_cpp=element_type.cpp,
        is_integer=element_type.is_integer,
        is_real=element_type.is_real,
        is_logical=element_type.is_logical,
        is_character=element_type.is_character,
    )


def _lower_data_statements(
    node: Node, locals_: list[IRLocal]
) -> list[IRStatement]:
    """Turn ``data`` statements into initializing assignments.

    ``data a /1,2,3/`` (a is an array) becomes ``a = [1,2,3]``;
    ``data n, x /5, 3.14/`` becomes ``n = 5; x = 3.14;``.  Values are
    distributed across objects left-to-right; an array object consumes
    the remaining values (correct when it's the last/only object).

    Scanned unit-wide (not just the specification part) since F77 permits
    DATA among executable statements; those are dropped where they appear
    (see _lower_action_statement) and collected here instead.
    """
    arrays = {loc.name for loc in locals_ if loc.type.is_array}
    out: list[IRStatement] = []
    for ds in _unit_descendants(node, "DataStmt"):
        for dset in ds.children_of_kind("DataStmtSet"):
            objs = dset.children_of_kind("DataStmtObject")
            values: list[IRExpr] = []
            for v in dset.children_of_kind("DataStmtValue"):
                values.extend(_lower_data_value(v))
            vi = 0
            for obj in objs:
                # ``data ((a(i,j),i=1,n),j=1,m) /...values.../`` — an
                # implied-do visiting an array slice element-by-element.
                # Emit one assignment per element so the converter doesn't
                # need to materialize the whole array's worth of values up
                # front; anything more elaborate (jagged subscripts) falls
                # through to the plain object handling.
                ido = obj.first_child("DataImpliedDo")
                if ido is not None:
                    assignments = _expand_data_implied_do(ido, values, vi)
                    if assignments is not None:
                        out.extend(a for a, _ in assignments)
                        vi += sum(c for _, c in assignments)
                        continue
                    # An implied-do shape we can't enumerate (e.g. a
                    # computed subscript or a non-constant bound).  Fail
                    # loudly rather than silently leave the array zero — a
                    # silent drop is exactly the class of bug that produced
                    # wrong numerics before.
                    _data_todo(obj)  # raises ConversionError
                var = obj.first_child("Variable")
                expr = _lower_expression(var) if var is not None else None
                if expr is None:
                    _data_todo(obj)  # raises ConversionError
                if isinstance(expr, IRName) and expr.name in arrays:
                    # A whole-array object consumes the remaining values.
                    rest = values[vi:]
                    out.append(
                        IRAssignment(
                            target=expr,
                            value=IRArrayConstructor(elements=tuple(rest)),
                        )
                    )
                    vi = len(values)
                elif vi < len(values):
                    # A scalar, or a single array element / substring /
                    # component (``data a(2) /7.0/``, ``data s(1:3) /'abc'/``)
                    # — exactly one value.  (Previously a non-IRName target
                    # here was silently dropped, leaving the slot zero.)
                    out.append(IRAssignment(target=expr, value=values[vi]))
                    vi += 1
    return out


def _data_todo(obj: Node) -> NoReturn:
    """A DATA object the converter can't lower.  Fail loudly rather than
    silently leave the target at its default value."""
    src = obj.source.text if obj.source is not None else ""
    raise ConversionError(
        "this DATA initializer",
        note="unsupported DATA object",
        source=src,
    )


def _folded_int_text(text: str | None) -> int | None:
    """Parse a flang-folded integer constant (``"4_4"``, ``"-1_4"``,
    ``"7"``) to an int, or ``None`` if it isn't a plain integer (e.g. a
    variable reference or an un-folded expression)."""
    if not text:
        return None
    try:
        return int(text.split("_")[0])
    except ValueError:
        return None


def _expand_data_implied_do(
    ido: Node, values: list[IRExpr], start: int
) -> list[tuple[IRStatement, int]] | None:
    """Expand a ``DataImpliedDo`` over an array slice into individual
    element assignments.  Returns ``[(assignment, values_consumed), ...]``
    or ``None`` if the shape is too elaborate to enumerate here.

    Iteration is innermost-first (matching Fortran's column-major loop
    nesting), so ``((a(i,j),i=1,n),j=1,m)`` produces ``a(1,1)``,
    ``a(2,1)`` ... ``a(n,1)``, ``a(1,2)`` ... — the order DATA values are
    listed in source."""
    # Collect every loop variable name anywhere in the nest.  Bounds are
    # evaluated lazily against the current environment (an inner bound may
    # name an *outer* loop variable -- the triangular form
    # ``((C(N,M), M=0,N), N=1,K)`` where M runs 0..N).
    loop_var_names: set[str] = set()

    def _collect_vars(node: Node) -> None:
        lb = node.first_child("LoopBounds")
        if lb is not None:
            sc = lb.children_of_kind("Scalar")
            vn = sc[0].find_first("Name") if sc else None
            if vn is not None and vn.fortran:
                loop_var_names.add(vn.fortran)
        for do_obj in node.children_of_kind("DataIDoObject"):
            nested = do_obj.first_child("DataImpliedDo")
            if nested is not None:
                _collect_vars(nested)
    _collect_vars(ido)

    def eval_bound(scalar: Node, env: dict[str, int]) -> int | None:
        """Evaluate a loop-bound ``Scalar`` to an int: a folded constant, or
        a bare reference to an enclosing loop variable (current value in
        ``env``).  Returns None for anything else (forces the loud fallback)."""
        expr = scalar.find_first("Expr")
        txt = (expr.fortran or "").strip() if expr is not None else ""
        c = _folded_int_text(txt)
        if c is not None:
            return c
        m = re.fullmatch(
            r"(?:__builtin_int\(\s*)?([A-Za-z_]\w*)(?:\s*,\s*kind=\d+\s*\))?",
            txt,
        )
        if m is not None and m.group(1) in env:
            return env[m.group(1)]
        return None

    def parse_object(do_obj: Node) -> tuple[str, list[tuple[str, object]]] | None:
        """Parse one ``DataIDoObject`` array element into its name and the
        per-dimension subscript pattern.  Each subscript is either a loop
        variable (it advances) or a constant integer that stays fixed
        (``(C(1,1,J),J=1,81)`` -> the slice ``C(1,1,*)``).  Returns None for
        anything we can't enumerate (a compound subscript like ``a(2*i)``,
        or an object that isn't a plain array element)."""
        ae = do_obj.find_first("ArrayElement")
        if ae is None:
            return None
        name_node = ae.find_first("Name")
        if name_node is None or not name_node.fortran:
            return None
        arr_name = _safe_name(name_node.fortran)
        subs: list[tuple[str, object]] = []  # ('var', name) | ('const', int)
        for ss in ae.children_of_kind("SectionSubscript"):
            sexpr = ss.find_first("Expr")
            txt = (sexpr.fortran or "").strip() if sexpr is not None else ""
            const_val = _folded_int_text(txt)
            if const_val is not None:
                subs.append(("const", const_val))
                continue
            # A bare loop-variable reference, possibly wrapped by the
            # analyzer as ``__builtin_int(i,kind=4)``.
            m = re.fullmatch(
                r"(?:__builtin_int\(\s*)?([A-Za-z_]\w*)(?:\s*,\s*kind=\d+\s*\))?",
                txt,
            )
            if m is not None and m.group(1) in loop_var_names:
                subs.append(("var", m.group(1)))
                continue
            return None
        return arr_name, subs

    # Expand the *actual node tree* recursively.  A DataImpliedDo has a loop
    # variable and, per iteration, a sequence of DataIDoObject children --
    # each either a plain array element (emit one value) or a *nested*
    # DataImpliedDo (recurse).  Processing the children in source order per
    # iteration reproduces exactly how Fortran interleaves the DATA values.
    # This handles both the flat multi-object form
    # ``(NAMLST(I), LB(I), UB(I), I=1,N)`` and the mixed nested form
    # ``((SMPN(J,I), J=1,3), SMPC(I), I=1,N)`` where an outer level lists a
    # nested implied-do *and* a plain element together (the latter was
    # silently dropped by the previous innermost-only flattening).
    out: list[tuple[IRStatement, int]] = []
    env: dict[str, int] = {}
    failed = [False]

    def process(node: Node) -> None:
        if failed[0]:
            return
        lb = node.first_child("LoopBounds")
        scalars = lb.children_of_kind("Scalar") if lb is not None else []
        var_node = scalars[0].find_first("Name") if scalars else None
        if var_node is None or not var_node.fortran or len(scalars) < 3:
            failed[0] = True
            return
        var = var_node.fortran
        step_node = scalars[3] if len(scalars) >= 4 else None
        lo = eval_bound(scalars[1], env)
        hi = eval_bound(scalars[2], env)
        st = eval_bound(step_node, env) if step_node is not None else 1
        if lo is None or hi is None or st is None or st == 0:
            failed[0] = True
            return
        do_objs = node.children_of_kind("DataIDoObject")
        i = lo
        while (st > 0 and i <= hi) or (st < 0 and i >= hi):
            env[var] = i
            for do_obj in do_objs:
                if failed[0]:
                    return
                nested = do_obj.first_child("DataImpliedDo")
                if nested is not None:
                    process(nested)
                    continue
                parsed = parse_object(do_obj)
                if parsed is None:
                    failed[0] = True
                    return
                if start + len(out) >= len(values):
                    return  # values exhausted -- stop emitting
                arr_name, subs = parsed
                idx_str = ", ".join(
                    str(val) if kind == "const" else str(env[val])
                    for kind, val in subs
                )
                out.append((
                    IRAssignment(
                        target=IRRaw(f"{arr_name}({idx_str})"),
                        value=values[start + len(out)],
                    ),
                    1,
                ))
            i += st

    process(ido)
    if failed[0]:
        return None
    return out


def _lower_data_value(value_node: Node) -> list[IRExpr]:
    """Lower one ``DataStmtValue`` to its constant(s).

    A ``DataStmtRepeat`` child (``3*7``) repeats the constant that many
    times, so this returns a list."""
    dc = value_node.first_child("DataStmtConstant")
    if dc is None:
        return [IRRaw("0")]
    # A ``DataStmtConstant`` is, per the standard, always a literal or
    # named constant (optionally signed, optionally repeated) -- never a
    # folded expression.  flang attaches the *folded* value to the node's
    # ``fortran`` field (e.g. ``0.05`` -> ``"5.00000007450580...e-2_4"``,
    # the exact decimal of the nearest float), which is lossless but
    # unreadable.  The structural child literal preserves the original
    # source spelling (``0.05``), so lower that when present and fall back
    # to the folded spelling only when there is no structural child to
    # lower (defensive -- shouldn't happen for a well-formed DATA stmt).
    val: IRExpr
    inner = next(iter(dc.children), None)
    if inner is not None:
        # A signed constant (``-0.05``) is a ``Signed...`` wrapper whose
        # ``Sign`` node carries no text and whose magnitude sits a level
        # down under a plain ``...LiteralConstant``.  Drill to the
        # underlying literal so the magnitude (and its kind/``f`` suffix)
        # lower correctly, then recover the sign from the constant's
        # source text (``DataStmtConstant.source`` keeps the original
        # spelling, sign included) -- prepend to a literal for clean C++,
        # else wrap in a unary minus.  A named-constant value (``pi``)
        # matches no literal kind, so it lowers via ``inner`` unchanged.
        target = inner
        lit = inner.find_first(
            "RealLiteralConstant", "IntLiteralConstant",
            "ComplexLiteralConstant", "CharLiteralConstant",
            "LogicalLiteralConstant", "BOZLiteralConstant",
        )
        if lit is not None:
            target = lit
        val = _lower_expression(target)
        src = (dc.source.text if dc.source else "").strip()
        if src.startswith("-"):
            if isinstance(val, IRLiteral) and not val.cpp_text.startswith("-"):
                val = IRLiteral(cpp_text="-" + val.cpp_text,
                                cpp_type=val.cpp_type)
            elif not isinstance(val, IRLiteral):
                val = IRUnaryOp(op="-", operand=val)
    elif dc.fortran:
        val = IRLiteral(cpp_text=_format_data_constant(dc.fortran))
    else:
        val = IRRaw("0")
    count = 1
    repeat = value_node.first_child("DataStmtRepeat")
    if repeat is not None:
        count = _const_int(repeat)
        if count is None:
            count = 1
    return [val] * count


def _parse_int_literal(text: str | None) -> int | None:
    """Parse a Fortran integer literal possibly carrying a ``_kind`` suffix
    (``"128_4"`` -> 128)."""
    if not text:
        return None
    try:
        return int(text.split("_")[0])
    except ValueError:
        return None


def _const_int(node: Node) -> int | None:
    """Best-effort constant integer value of a node subtree: a literal, a
    flang-folded scalar ``value``, or a PARAMETER reference's ``init``.
    Used for e.g. a ``DATA`` repeat count that names a constant
    (``DATA V / N * 0.0 /`` with ``PARAMETER (N=128)``), which is otherwise
    invisible to a literal-only scan."""
    lit = node.find_first("IntLiteralConstant")
    if lit is not None:
        v = _parse_int_literal(lit.fortran)
        if v is not None:
            return v
    # A folded scalar-int ``value`` on any expression-bearing descendant.
    stack = [node]
    while stack:
        n = stack.pop()
        if n.value is not None:
            v = _parse_int_literal(n.value)
            if v is not None:
                return v
        if n.init is not None:
            v = _parse_int_literal(n.init)
            if v is not None:
                return v
        stack.extend(n.children)
    return None


def _format_data_constant(text: str) -> str:
    """Render a flang-folded scalar constant (``"3.5e-1_4"``,
    ``"42_4"``, ``"-1._8"``, ``'"hello"'``, ``".true._4"``) as C++
    literal text.  The trailing ``_kind`` suffix encodes Fortran KIND
    and is stripped; an ``f`` suffix is added for single-precision
    reals.  String constants ``'"..."'`` and logicals (``.true._4``)
    are passed through after kind-suffix stripping."""
    s = text.strip()
    # Character literals come quoted; emit them as ``std::string_view``
    # so they assign cleanly into a ``FortranString<N>`` slot the same
    # way other character expressions do.
    if s.startswith('"'):
        return s + "sv"
    if s.startswith("'"):
        # Normalize to double quotes for C++ (single quotes are character
        # literals there) and append the sv suffix.
        return '"' + s[1:-1].replace('"', '\\"') + '"sv'
    # Logical literals: flang renders ``.true._4`` / ``.false._4``.
    low = s.lower()
    if low.startswith(".true.") or low.startswith(".false."):
        return "true" if low.startswith(".true.") else "false"
    kind: int | None = None
    if "_" in s:
        base, _, kbits = s.rpartition("_")
        try:
            kind = int(kbits)
            s = base
        except ValueError:
            pass
    is_real = ("." in s) or ("e" in s) or ("E" in s)
    if is_real:
        if kind is None or kind == 4:
            return s + "f"
        if kind == 8:
            return s
        if kind == 16:
            return s + "L"
        return s + "f"
    return s


def _lower_use_statements(spec_part: Node) -> list[str]:
    """Collect the module names imported by ``use`` statements."""
    out: list[str] = []
    for use in spec_part.find_all("UseStmt"):
        name = use.find_first("Name")
        if name is not None and name.fortran:
            out.append(_safe_name(name.fortran))
    return out


def _drop_equiv_aliased_locals(sub: "IRSubprogram") -> None:
    """Remove every IRLocal that's been turned into an equivalence alias.

    Two cases produced aliases:
      * a member of a pun group -- its storage now lives in the equiv
        struct;
      * a same-type EQUIVALENCE rename -- its storage is the canonical
        primary, reached via a state-binding (param == "").
    Either way the original IRLocal is no longer the declaration site,
    so drop it from ``sub.locals`` to keep emit from re-declaring it."""
    pun_aliased = {m.name for g in sub.equiv_groups for m in g.members}
    rename_aliased = {b.name for b in sub.state_bindings if not b.param}
    aliased = pun_aliased | rename_aliased
    if aliased:
        sub.locals = [loc for loc in sub.locals if loc.name not in aliased]


def _lower_equivalence_statements(
    spec_part: Node, sub: "IRSubprogram"
) -> list["IREquivGroup"]:
    """Collect ``equivalence (a, b, ...)`` groups and lower them.

    We distinguish two cases.  When every member of a group has the same
    element type *and* the same byte size, the equivalence is just a
    renaming for memory sharing -- no type punning is needed.  Lowering
    keeps the first member's IRLocal as the canonical storage and adds a
    reference-binding from each alias to it (zero memory overhead, no
    proxy machinery).  When the members differ (a ``DOUBLE PRECISION``
    array aliased to an ``INTEGER`` one -- SPICE's DAF type-pun pattern),
    we build an :class:`IREquivGroup` whose emitted struct holds a single
    shared byte buffer plus a typed proxy per member.

    Only whole-object aliases of arithmetic scalars, 1-D static
    arithmetic arrays, or CHARACTER variables are supported; anything
    more elaborate (a partial subscript, COMMON-block placement,
    higher-rank aliases) raises :class:`ConversionError`."""
    out: list[IREquivGroup] = []
    locals_by_name = {loc.name: loc for loc in sub.locals}
    # ``EquivalenceStmt`` -> one or more ``EquivalenceSet`` children, each
    # a group of objects that all share the same storage.  The dumper
    # wraps each parenthesized ``(a, b, ...)`` group in EquivalenceSet so
    # multi-group statements like ``EQUIVALENCE (A, B), (C, D)`` stay
    # distinguishable.
    for stmt in spec_part.find_all("EquivalenceStmt"):
        for s_idx, eset in enumerate(stmt.children_of_kind("EquivalenceSet")):
            objs = list(eset.children_of_kind("EquivalenceObject"))
            if len(objs) < 2:
                continue  # a single-member equivalence is a no-op
            # Decode each object into a (local_name, access_text,
            # scalar_cpp_type, byte_size) descriptor.  A bare scalar / 1-D
            # array uses its declaration as the access text; an
            # ArrayElement with a constant subscript (``ptr(1)``) reduces
            # to one element of the array -- ``access`` becomes
            # ``"ptr(1)"`` and the descriptor describes that element's
            # scalar type, leaving the parent array intact (it provides
            # the actual storage; the alias is a reference to its slot).
            descs: list[
                tuple[str, str, str, int, "IREquivMember | None"]
            ] = []
            for obj in objs:
                if obj.find_first("Substring") is not None:
                    raise ConversionError(
                        "EQUIVALENCE",
                        note="substring overlap is not supported",
                        source=stmt.source.text if stmt.source else "",
                    )
                ae = obj.find_first("ArrayElement")
                if ae is not None:
                    desc = _equiv_element_alias_descriptor(
                        ae, locals_by_name, stmt
                    )
                    descs.append(desc)
                    continue
                bare_name = obj.find_first("Name")
                if bare_name is None or not bare_name.fortran:
                    raise ConversionError(
                        "EQUIVALENCE",
                        note="malformed object (no resolvable name)",
                        source=stmt.source.text if stmt.source else "",
                    )
                local_name = _safe_name(bare_name.fortran)
                loc = locals_by_name.get(local_name)
                if loc is None:
                    raise ConversionError(
                        "EQUIVALENCE",
                        note=f"member {local_name!r} not found as a local",
                        source=stmt.source.text if stmt.source else "",
                    )
                m = _equiv_member_from_local(loc, stmt, sub.locals)
                descs.append((m.name, m.name, m.cpp_elem_type,
                              _member_byte_size(m), m))
            # No-pun case: every descriptor is the same element type AND
            # same byte size.  Pick a storage *anchor* whose access text
            # is an existing storage slot we don't want to drop -- prefer
            # an element designator (``ptr(1)`` -- ``ptr``'s storage
            # already exists), then a whole-object designator (the
            # straightforward rename).  Every bare-name alias that isn't
            # the anchor's own local becomes ``auto& alias = <anchor
            # access>;`` and its local is dropped.  The anchor itself
            # (whether it's an array or a bare scalar) stays.
            same_type_size = all(
                d[2] == descs[0][2] and d[3] == descs[0][3] for d in descs[1:]
            )
            if same_type_size:
                anchor_idx = next(
                    (i for i, d in enumerate(descs) if d[0] != d[1]),
                    0,
                )
                anchor_local = descs[anchor_idx][0]
                anchor_access = descs[anchor_idx][1]
                for i, (lname, access, _ty, _bs, _m) in enumerate(descs):
                    if i == anchor_idx:
                        continue
                    if access != lname:
                        # An element designator on a *different* array
                        # that resolves to the same slot is a pun shape
                        # we don't model -- fall through.
                        break
                    if lname == anchor_local:
                        continue
                    sub.state_bindings.append(
                        IRStateBinding(
                            name=lname, param="", field=anchor_access
                        )
                    )
                    locals_by_name.pop(lname, None)
                else:
                    continue
            # Pun case: build an IREquivGroup over the shared bytes.
            # Element-subscript aliases need a partial-buffer view we
            # don't model; reject them so the gap is loud.
            members = [d[4] for d in descs]
            if any(m is None for m in members):
                raise ConversionError(
                    "EQUIVALENCE",
                    note=(
                        "element-subscript alias with a type-pun pattern "
                        "is not supported"
                    ),
                    source=stmt.source.text if stmt.source else "",
                )
            byte_size = max(_member_byte_size(m) for m in members)
            alignment = max(m.alignment for m in members)
            cpp_type = f"{camelcase(sub.display_name)}_Equiv{len(out) + 1}"
            out.append(
                IREquivGroup(
                    cpp_type=cpp_type,
                    byte_size=byte_size,
                    alignment=alignment,
                    members=members,
                )
            )
    return out


_EQUIV_ELEM_INFO: dict[str, tuple[str, int]] = {
    # cpp scalar type -> (alignment-bytes, size-bytes)
    "bool": (1, 1),
    "char": (1, 1),
    "int8_t": (1, 1),
    "int16_t": (2, 2),
    "int32_t": (4, 4),
    "int64_t": (8, 8),
    "float": (4, 4),
    "double": (8, 8),
}


def _equiv_element_alias_descriptor(
    ae: Node,
    locals_by_name: dict[str, "IRLocal"],
    stmt: Node,
) -> tuple[str, str, str, int, None]:
    """For an ``EquivalenceObject`` shaped ``arr(idx)`` (an ArrayElement),
    return ``(arr_name, "arr(idx)", elem_cpp_type, sizeof_elem, None)``.

    ``arr`` must be a 1-D arithmetic array local (the storage we're
    aliasing into), ``idx`` must be a single constant integer subscript
    (Fortran 1-based).  The trailing ``None`` matches the descriptor
    tuple shape used by whole-object descriptors (where the last slot
    carries an IREquivMember the pun path can use)."""
    name_node = ae.find_first("Name")
    if name_node is None or not name_node.fortran:
        raise ConversionError(
            "EQUIVALENCE",
            note="malformed array-element alias",
            source=stmt.source.text if stmt.source else "",
        )
    arr_name = _safe_name(name_node.fortran)
    loc = locals_by_name.get(arr_name)
    if loc is None:
        raise ConversionError(
            "EQUIVALENCE",
            note=f"array {arr_name!r} not found as a local",
            source=stmt.source.text if stmt.source else "",
        )
    t = loc.type
    if not (t.is_array and t.array_rank == 1):
        raise ConversionError(
            "EQUIVALENCE",
            note=(
                f"element alias {arr_name!r} requires a 1-D arithmetic "
                "array; higher ranks are not supported"
            ),
            source=stmt.source.text if stmt.source else "",
        )
    elem_cpp = t.element_type_cpp or t.cpp
    info = _EQUIV_ELEM_INFO.get(elem_cpp)
    if info is None:
        raise ConversionError(
            "EQUIVALENCE",
            note=f"element alias on type {elem_cpp!r} is not supported",
            source=stmt.source.text if stmt.source else "",
        )
    _align, size = info
    # Single constant subscript only.
    subs = ae.children_of_kind("SectionSubscript")
    if len(subs) != 1:
        raise ConversionError(
            "EQUIVALENCE",
            note="element alias must have exactly one subscript",
            source=stmt.source.text if stmt.source else "",
        )
    lit = subs[0].find_first("IntLiteralConstant")
    if lit is None or not lit.fortran:
        raise ConversionError(
            "EQUIVALENCE",
            note="element alias subscript must be a constant integer",
            source=stmt.source.text if stmt.source else "",
        )
    try:
        idx = int(lit.fortran.split("_")[0])
    except ValueError:
        raise ConversionError(
            "EQUIVALENCE",
            note=f"element alias subscript {lit.fortran!r} is not an integer",
            source=stmt.source.text if stmt.source else "",
        )
    access = f"{arr_name}({idx})"
    return (arr_name, access, elem_cpp, size, None)


def _equiv_member_from_local(
    loc: "IRLocal", stmt: Node, locals_: list["IRLocal"]
) -> "IREquivMember":
    """Build an :class:`IREquivMember` from a local declaration.

    Supports a CHARACTER string, an arithmetic scalar, and an arithmetic
    1-D static-bound array.  Rejects anything else with a
    :class:`ConversionError` so unsupported shapes surface immediately.
    """
    t = loc.type
    if t.is_character:
        n = _character_length_from_cpp(t.cpp)
        if n is None:
            raise ConversionError(
                "EQUIVALENCE",
                note=(f"CHARACTER member {loc.name!r} has unsupported "
                      "length spelling " + repr(t.cpp)),
                source=stmt.source.text if stmt.source else "",
            )
        return IREquivMember(
            name=loc.name,
            cpp_elem_type="char",
            count=n,
            alignment=1,
            is_character=True,
        )
    if t.is_array:
        if t.array_rank != 1:
            raise ConversionError(
                "EQUIVALENCE",
                note=f"array member {loc.name!r} must be 1-D",
                source=stmt.source.text if stmt.source else "",
            )
        elem_cpp = t.element_type_cpp or t.cpp
        info = _EQUIV_ELEM_INFO.get(elem_cpp)
        if info is None:
            raise ConversionError(
                "EQUIVALENCE",
                note=(f"array element type {elem_cpp!r} on member "
                      f"{loc.name!r} is not supported"),
                source=stmt.source.text if stmt.source else "",
            )
        align, _size = info
        # ``_array_static_extent`` resolves a constant integer or a
        # bare PARAMETER reference (the SPICE ``DPBUFR(DPBLEN)``
        # pattern); a non-constant bound is unsupported.
        n = _array_static_extent(t, locals_)
        if n is None:
            raise ConversionError(
                "EQUIVALENCE",
                note=(f"array member {loc.name!r} extent is not a "
                      "compile-time integer constant"),
                source=stmt.source.text if stmt.source else "",
            )
        return IREquivMember(
            name=loc.name,
            cpp_elem_type=elem_cpp,
            count=n,
            alignment=align,
        )
    # Arithmetic scalar.
    info = _EQUIV_ELEM_INFO.get(t.cpp)
    if info is None:
        raise ConversionError(
            "EQUIVALENCE",
            note=(f"scalar member {loc.name!r} of C++ type {t.cpp!r} is "
                  "not supported"),
            source=stmt.source.text if stmt.source else "",
        )
    align, _size = info
    return IREquivMember(
        name=loc.name, cpp_elem_type=t.cpp, count=None, alignment=align
    )


def _character_length_from_cpp(cpp: str) -> int | None:
    """``"ftn::FortranString<8>"`` -> ``8``."""
    m = re.match(r"ftn::FortranString<\s*(\d+)\s*>", cpp)
    return int(m.group(1)) if m is not None else None


def _array_static_extent(
    t: "IRType", locals_: list["IRLocal"] | None = None
) -> int | None:
    """Pull the constant element count from a 1-D static Array IRType.

    The extent lives on ``IRType.array_extent_exprs[0]`` as a rendered
    C++ string; we accept a plain integer, a bare PARAMETER name, or a
    small arithmetic expression that folds through PARAMETER references
    (the SPICE ``INBLEN = DPLEN/2 * 2`` pattern)."""
    if not t.array_extent_exprs or len(t.array_extent_exprs) != 1:
        return None
    txt = t.array_extent_exprs[0].strip()
    try:
        return int(txt)
    except ValueError:
        pass
    if locals_ is None:
        return None
    params_by_name = {
        loc.name: loc for loc in locals_ if loc.is_parameter
    }
    # Walk the PARAMETER's initializer expression tree, folding through
    # references to other PARAMETERs.
    if txt.isidentifier():
        loc = params_by_name.get(txt)
        if loc is None:
            return None
        return _fold_const_int(loc.initializer, params_by_name, set())
    return None


def _fold_const_int(
    expr: "IRExpr | None",
    params_by_name: dict[str, "IRLocal"],
    seen: set[str],
) -> int | None:
    """Best-effort constant-fold ``expr`` to an int, following PARAMETER
    references in ``params_by_name``.  Cycle-safe via ``seen``."""
    if expr is None:
        return None
    if isinstance(expr, IRLiteral):
        try:
            return int(re.sub(r"[fLu]+$", "", expr.cpp_text))
        except ValueError:
            return None
    if isinstance(expr, IRRaw):
        try:
            return int(re.sub(r"[fLu]+$", "", expr.text.strip()))
        except ValueError:
            return None
    if isinstance(expr, IRName):
        if expr.name in seen:
            return None
        loc = params_by_name.get(expr.name)
        if loc is None:
            return None
        return _fold_const_int(
            loc.initializer, params_by_name, seen | {expr.name}
        )
    if isinstance(expr, IRBinaryOp):
        lhs = _fold_const_int(expr.lhs, params_by_name, seen)
        rhs = _fold_const_int(expr.rhs, params_by_name, seen)
        if lhs is None or rhs is None:
            return None
        try:
            return {
                "+": lambda: lhs + rhs,
                "-": lambda: lhs - rhs,
                "*": lambda: lhs * rhs,
                "/": lambda: lhs // rhs if rhs != 0 else None,
            }.get(expr.op, lambda: None)()
        except Exception:
            return None
    if isinstance(expr, IRUnaryOp):
        v = _fold_const_int(expr.operand, params_by_name, seen)
        if v is None:
            return None
        if expr.op == "-":
            return -v
        if expr.op == "+":
            return v
    return None


def _member_byte_size(m: "IREquivMember") -> int:
    info = _EQUIV_ELEM_INFO.get(m.cpp_elem_type, (1, 1))
    elem_bytes = info[1]
    return elem_bytes * (m.count if m.count is not None else 1)


def _lower_common_statements(spec_part: Node) -> list[IRCommonUse]:
    """Collect ``common /name/ a, b, c`` declarations.

    AST shape: ``CommonStmt -> Block -> [Name (block), CommonBlockObject*]``
    where the first ``Name`` in the inner Block is the block name and
    each ``CommonBlockObject`` names a member.  A blank common block
    has no leading Name.
    """
    out: list[IRCommonUse] = []
    for common_stmt in spec_part.find_all("CommonStmt"):
        for block in common_stmt.children_of_kind("Block"):
            block_name = ""
            members: list[str] = []
            # A leading bare Name (not wrapped in CommonBlockObject) is
            # the block name.
            leading_name = next(
                (c for c in block.children if c.kind == "Name"), None
            )
            if leading_name is not None and leading_name.fortran:
                block_name = _safe_name(leading_name.fortran)
            for obj in block.children_of_kind("CommonBlockObject"):
                name = obj.find_first("Name")
                if name is not None and name.fortran:
                    members.append(_safe_name(name.fortran))
            out.append(
                IRCommonUse(block_name=block_name, member_names=members)
            )
    return out


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def _lower_specification(spec_part: Node) -> list[IRLocal]:
    """Lower a SpecificationPart's declarations into IRLocal entries.

    Walk Statement-wrappers (rather than directly drilling into
    TypeDeclarationStmt) so the wrapping Statement's leading and
    trailing comments survive — they would otherwise be lost.
    """
    out: list[IRLocal] = []
    for stmt in spec_part.find_all("Statement"):
        decl = stmt.find_first("TypeDeclarationStmt")
        if decl is None:
            continue
        leading = list(stmt.leading_comments)
        trailing = list(stmt.trailing_comments)
        locals_ = _lower_type_declaration(decl)
        # Apply the wrapping Statement's comments to the first / last
        # local in the group respectively, so multi-name declarations
        # ``integer :: a, b, c   ! triple of counters`` keep the
        # comment paired with the right line.
        if locals_:
            locals_[0].leading_comments = leading
            locals_[-1].trailing_comments = trailing
        out.extend(locals_)
    # Named constants from ``parameter (...)`` statements become constexpr
    # locals.  They go first so attribute-form initializers and array
    # bounds that reference them are already declared; a name given a type
    # *and* a PARAMETER value (``integer n`` + ``parameter (n=5)``) keeps
    # only the constexpr form.
    params = _lower_parameter_statements(spec_part)
    param_names = {p.name for p in params}
    # Statement functions become generic lambdas, declared last so they can
    # capture the locals they reference.  A preceding type declaration of the
    # same name (``logical isquot`` before ``isquot(code) = ...``) only states
    # the function's result type, not a variable, so drop that plain local —
    # otherwise it conflicts with the lambda.
    stmt_funcs = _lower_statement_functions(spec_part)
    sf_names = {sf.name for sf in stmt_funcs}
    decls = [
        loc for loc in out if loc.name not in param_names and loc.name not in sf_names
    ]
    return params + decls + stmt_funcs


def _lower_statement_functions(spec_part: Node) -> list[IRLocal]:
    """``f(x, y) = x*x + y`` -> ``auto f = [&](auto x, auto y){ return
    x*x + y; };`` (a generic lambda capturing host locals by reference)."""
    out: list[IRLocal] = []
    for sf in spec_part.find_all("StmtFunctionStmt"):
        names = [c for c in sf.children if c.kind == "Name"]
        if not names or not names[0].fortran:
            continue
        fname = _safe_name(names[0].fortran)
        params = tuple(
            _safe_name(n.fortran) for n in names[1:] if n.fortran
        )
        scalar = sf.first_child("Scalar")
        expr = scalar.find_first("Expr") if scalar is not None else None
        if expr is None:
            continue
        out.append(
            IRLocal(
                name=fname,
                type=IRType(cpp="auto", fortran="statement function"),
                initializer=IRLambda(params=params, body=_lower_expression(expr)),
            )
        )
    return out


def _lower_parameter_statements(spec_part: Node) -> list[IRLocal]:
    """``parameter (n=5, pi=3.14)`` -> constexpr locals.

    The name carries its resolved type; the value is the lowered constant
    expression.  (The attribute form ``integer, parameter :: n=5`` is
    handled by the type-declaration path instead.)"""
    out: list[IRLocal] = []
    for pstmt in spec_part.find_all("ParameterStmt"):
        for ncd in pstmt.children_of_kind("NamedConstantDef"):
            nc = ncd.first_child("NamedConstant")
            name = nc.first_child("Name") if nc is not None else None
            const = ncd.first_child("Constant")
            expr = const.find_first("Expr") if const is not None else None
            if name is None or not name.fortran or expr is None:
                continue
            ty = (
                _scalar_type_from_fortran(name.sym_type)
                if name.sym_type
                else None
            ) or IRType(cpp="auto", fortran="parameter")
            out.append(
                IRLocal(
                    name=_safe_name(name.fortran),
                    type=ty,
                    is_parameter=True,
                    initializer=_lower_expression(expr),
                )
            )
    return out


def _lower_type_declaration(decl: Node) -> list[IRLocal]:
    """One ``TypeDeclarationStmt`` may declare several names sharing a type."""
    type_node = decl.first_child("DeclarationTypeSpec")
    if type_node is None:
        return []
    decl_ir_type = lower_type_spec(type_node)

    # Attribute flags come from each entity's resolved-symbol attrs below
    # — that picks up both inline (``REAL, INTENT(IN) :: x``) and standalone
    # (``INTENT(IN) :: x`` after a separate ``REAL :: x``) forms uniformly,
    # since flang's symbol consolidates them.  The one attr we read
    # syntactically is EXTERNAL on the declaration itself: that form
    # (``real, external :: f``) declares ``f`` a procedure, so the whole
    # decl is dropped (a *standalone* ``EXTERNAL f`` after ``REAL :: f``
    # keeps the local for the later parameter-separation pass to drop in a
    # context that knows about sibling-ENTRY procedure-dummy fall-through).
    # ArraySpec ("dimension(...)") is a shape, not an attr — propagates to
    # every EntityDecl that lacks its own ArraySpec.
    shared_array_spec: Node | None = None
    decl_is_external = False
    for attr in decl.find_all("AttrSpec"):
        for child in attr.children:
            if child.kind == "ArraySpec":
                shared_array_spec = child
            elif child.kind == "External":
                decl_is_external = True
    if decl_is_external:
        return []

    out: list[IRLocal] = []
    for entity in decl.children:
        if entity.kind != "EntityDecl":
            continue
        name_node = entity.first_child("Name")
        if name_node is None or not name_node.fortran:
            continue
        attrs = set(name_node.attrs)
        is_parameter = "parameter" in attrs
        is_save = "save" in attrs
        is_optional = "optional" in attrs
        is_pointer = "pointer" in attrs
        if "intent(in)" in attrs:
            intent: Literal["in", "out", "inout"] | None = "in"
        elif "intent(out)" in attrs:
            intent = "out"
        elif "intent(inout)" in attrs:
            intent = "inout"
        else:
            intent = None
        # Prefer flang's resolved symbol type when available: it substitutes
        # named-constant lengths/kinds that the raw declaration AST leaves
        # symbolic, e.g. ``CHARACTER*(NWC)`` -> ``CHARACTER(1024,1)``.
        ir_type = (
            _scalar_type_from_fortran(name_node.sym_type)
            if name_node.sym_type else None
        ) or decl_ir_type
        initializer = None
        init = entity.find_first("Initialization")
        if init is not None:
            expr = init.find_first("Expr") or init.find_first("ConstantExpr")
            if expr is not None:
                initializer = _lower_expression(expr)
        per_entity_array = entity.first_child("ArraySpec")
        array_spec = per_entity_array if per_entity_array is not None else shared_array_spec
        if array_spec is not None:
            loc_type = _make_array_type(ir_type, array_spec, is_pointer=is_pointer)
        elif is_pointer:
            # Scalar pointer -> raw C++ pointer.
            loc_type = IRType(
                cpp=f"{ir_type.cpp}*", fortran=f"{ir_type.fortran}, pointer",
                is_pointer=True,
                is_integer=ir_type.is_integer, is_real=ir_type.is_real,
                is_logical=ir_type.is_logical, is_character=ir_type.is_character,
                element_type_cpp=ir_type.cpp,
            )
        else:
            loc_type = ir_type
        out.append(
            IRLocal(
                name=_safe_name(name_node.fortran),
                type=loc_type,
                initializer=initializer,
                is_parameter=is_parameter,
                is_save=is_save,
                intent=intent,
                is_optional=is_optional,
                is_pointer=is_pointer,
                common_block=name_node.common_block,
                equivalence_class=name_node.equivalence_class,
            )
        )
    return out


def _assumed_dim_lower(dim_spec: Node) -> str:
    """The lower bound of one assumed-size dimension as a literal string.

    ``ExplicitShapeSpec`` (a leading explicit dim like the ``2`` in
    ``POOL(2, LBPOOL:*)``) -> its lower (default ``"1"``).
    ``AssumedImpliedSpec`` (``*`` or ``lo:*``) -> the ``lo`` bound, folded
    to a literal (``LBPOOL`` -> ``"-5"``) so static-lb extraction accepts
    it, or ``"1"`` for a bare ``*``.  A non-constant bound falls back to
    ``"1"`` (the view then resolves it at runtime via the actual)."""
    if dim_spec.kind == "ExplicitShapeSpec":
        lo, _ = _lower_explicit_shape(dim_spec)
        return lo if lo is not None else "1"
    # AssumedImpliedSpec: a present SpecificationExpr is the lower bound.
    se = dim_spec.first_child("SpecificationExpr")
    if se is not None:
        v = _const_int(se)
        if v is not None:
            return str(v)
    return "1"


def _assumed_dim_extent(dim_spec: Node) -> str:
    """The extent of one assumed-size dimension.  An explicit leading dim
    (``DLINES(DLSIZE, *)`` -> the ``DLSIZE``) has a real extent that must be
    kept -- losing it collapses sequence-association reshapes; only the
    trailing assumed (``*``) dimension is caller-sized (placeholder)."""
    if dim_spec.kind == "ExplicitShapeSpec":
        _, hi = _lower_explicit_shape(dim_spec)
        return hi
    return "/* assumed-size */ 0"


def _make_array_type(
    element_type: IRType, array_spec: Node, *, is_pointer: bool = False
) -> IRType:
    """Wrap ``element_type`` in ``ftn::Array<T, Rank>`` with
    extent / lower-bound expressions extracted from ``array_spec``.

    A POINTER deferred-shape array becomes a non-owning ``ArrayRef``."""
    extents: list[str] = []
    lowers: list[str] = []
    has_explicit_lower = False
    all_static = True
    for shape in array_spec.children:
        if shape.kind == "ExplicitShapeSpec":
            lo, hi = _lower_explicit_shape(shape)
            if lo is not None:
                # Fold a named-constant lower (``LBCELL`` -> ``-5``) so a cell
                # dummy gets a static ``Lower`` NTTP instead of the runtime
                # sentinel (see ``_fold_explicit_lower``).  Skip CHARACTER
                # arrays: a static-lower owning ``Array<FortranString,...>``
                # has no ``CharArrayRef`` conversion, so leave them in the
                # runtime form.
                if not element_type.is_character:
                    folded = _fold_explicit_lower(shape)
                    if folded is not None:
                        lo = folded
                lowers.append(lo)
                has_explicit_lower = True
            else:
                lowers.append("1")
            extents.append(hi)
            if not _explicit_shape_is_const(shape):
                all_static = False
        elif shape.kind == "DeferredShapeSpecList":
            # ``a(:)`` / ``a(:,:)`` — allocatable or pointer array.  The
            # rank is the ``int`` child; extents are unknown (no extents
            # -> default-constructed).  Pointer arrays are non-owning
            # ArrayRef views; allocatable arrays own their storage.
            rank_node = shape.first_child("int")
            try:
                rank_n = int(rank_node.fortran) if rank_node and rank_node.fortran else 1
            except ValueError:
                rank_n = 1
            cont = "ftn::ArrayRef" if is_pointer else "ftn::Array"
            return IRType(
                cpp=f"{cont}<{element_type.cpp}, {rank_n}>",
                fortran=f"{element_type.fortran}"
                + (", pointer" if is_pointer else ", allocatable"),
                is_array=True,
                array_rank=rank_n,
                array_extent_exprs=(),  # empty -> default-constructed
                array_static=False,
                is_pointer=is_pointer,
                element_type_cpp=element_type.cpp,
                is_integer=element_type.is_integer,
                is_real=element_type.is_real,
                is_logical=element_type.is_logical,
                is_character=element_type.is_character,
            )
        elif shape.kind == "AssumedSizeSpec":
            # ``a(m, n, *)`` / ``a(2, LBPOOL:*)`` — an assumed-size spec that
            # wraps the leading explicit dimensions plus the trailing assumed
            # one.  Extents are caller-sized (unknown), but each lower bound
            # *is* known from the declaration (default 1, or an explicit /
            # named-constant bound like ``LBPOOL``).  Capture them so the
            # dummy is a static-lower view -- a Fortran dummy indexes from its
            # *own* declared lb, not the actual's; without this the runtime
            # view inherits the actual's lb and ``POOL(_,0)`` overruns.
            dims = [
                c
                for c in shape.children
                if c.kind in ("ExplicitShapeSpec", "AssumedImpliedSpec")
            ]
            for c in dims or [shape]:
                extents.append(_assumed_dim_extent(c))
                lowers.append(_assumed_dim_lower(c))
            has_explicit_lower = True
            all_static = False
        elif shape.kind == "AssumedShapeSpec":
            # One ``:`` of an assumed-shape dummy (``a(:,:)`` -> one spec
            # per dimension); unknown extent.
            extents.append("/* assumed-shape */ 0")
            lowers.append("1")
            all_static = False
        elif shape.kind == "ImpliedShapeSpec":
            # The classic F77 assumed-size dummy ``a(*)`` (or ``a(lo:*)``,
            # ``a(m,*)``) parses as an implied-shape spec — one dimension
            # per ``AssumedImpliedSpec``.  Extent is caller-sized; the lower
            # bound is known (default 1 or an explicit ``lo``) and captured
            # as a static lb (see AssumedSizeSpec above).
            specs = list(shape.find_all("AssumedImpliedSpec"))
            for c in specs or [shape]:
                extents.append(_assumed_dim_extent(c))
                lowers.append(_assumed_dim_lower(c))
            has_explicit_lower = True
            all_static = False
    rank = len(extents)
    return IRType(
        cpp=f"ftn::Array<{element_type.cpp}, {rank}>",
        fortran=f"{element_type.fortran}, dimension({len(extents)})",
        is_array=True,
        array_rank=rank,
        array_extent_exprs=tuple(extents),
        array_lower_bound_exprs=tuple(lowers) if has_explicit_lower else (),
        array_static=all_static,
        element_type_cpp=element_type.cpp,
        is_integer=element_type.is_integer,
        is_real=element_type.is_real,
        is_logical=element_type.is_logical,
        is_character=element_type.is_character,
    )


def _explicit_shape_is_const(shape: Node) -> bool:
    """True when every bound in an ExplicitShapeSpec is a compile-time
    constant (so the array size is fixed and can be hoisted)."""
    for spec in shape.find_all("SpecificationExpr"):
        inner = spec.find_first("Expr")
        if inner is None or not _is_const_expr(_lower_expression(inner)):
            return False
    return True


def _is_const_expr(expr: IRExpr) -> bool:
    """Whether an IRExpr is a compile-time integer constant expression."""
    if isinstance(expr, IRLiteral):
        return True
    if isinstance(expr, IRBinaryOp):
        return _is_const_expr(expr.lhs) and _is_const_expr(expr.rhs)
    if isinstance(expr, IRUnaryOp):
        return _is_const_expr(expr.operand)
    return False


def _lower_explicit_shape(shape: Node) -> tuple[str | None, str]:
    """Return ``(lower_cpp, upper_or_extent_cpp)`` for an ExplicitShapeSpec.

    Fortran's ``a(10)`` has no lower bound (defaults to 1) and an
    upper bound of 10, so the extent is 10.  ``a(0:9)`` has an
    explicit lower of 0 and upper of 9, so the extent is 10.  We
    pass the *extent* to ``ftn::Array``, but keep the lower bound
    separate so the constructor can use the (lower, extent) form.
    """
    exprs = list(shape.find_all("SpecificationExpr"))
    if not exprs:
        return None, "0"
    if len(exprs) == 1:
        # ``a(N)`` — upper only, extent == N, lower == 1.
        upper = _render_spec_expr(exprs[0])
        return None, upper
    # ``a(lo:hi)`` — both bounds given.  Extent = hi - lo + 1.
    lower = _render_spec_expr(exprs[0])
    upper = _render_spec_expr(exprs[1])
    extent = f"({upper}) - ({lower}) + 1"
    return lower, extent


def _fold_explicit_lower(shape: Node) -> str | None:
    """Fold an ExplicitShapeSpec's lower bound to its integer literal when it
    is a *named* compile-time constant (the SPICE ``LBCELL = -5`` cell lower
    bound: ``WORK(LBCELL:MW, NW)``).  Such a name otherwise renders as itself,
    which can't appear in a dummy's type ``Lower`` NTTP (the constant isn't in
    scope at the signature) -- so the view fell back to the runtime sentinel,
    lost the -5, and ``WORK(-5,I)`` (the cell's control element) overran the
    ``[1,..]`` storage.  Returns the literal string, or ``None`` when the
    bound is absent, already a literal, or not constant-foldable.  (A bare
    ``-5`` already renders correctly and is left alone -- ``_const_int`` would
    drop its sign.)"""
    exprs = list(shape.find_all("SpecificationExpr"))
    if len(exprs) < 2:
        return None
    from .static_lower import try_static_lower_literals

    rendered = _render_spec_expr(exprs[0])
    if try_static_lower_literals((rendered,)) is not None:
        return None
    v = _const_int(exprs[0])
    return str(v) if v is not None else None


def _render_spec_expr(node: Node) -> str:
    """Lower a ``SpecificationExpr`` to a C++ expression string."""
    inner = node.find_first("Expr")
    if inner is None:
        return "0"
    return _render_expr_inline(_lower_expression(inner))


def _render_expr_inline(expr: IRExpr) -> str:
    """Render an IRExpr to C++ text — duplicates the emitter's
    rendering for use during lowering.  Kept here to avoid importing
    the emitter (which would create a cycle).
    """
    if isinstance(expr, IRLiteral):
        return expr.cpp_text
    if isinstance(expr, IRName):
        return expr.name
    if isinstance(expr, IRBinaryOp):
        return f"{_render_expr_inline(expr.lhs)} {expr.op} {_render_expr_inline(expr.rhs)}"
    if isinstance(expr, IRUnaryOp):
        if expr.op == "()":
            return f"({_render_expr_inline(expr.operand)})"
        return f"{expr.op}{_render_expr_inline(expr.operand)}"
    if isinstance(expr, IRFunctionCall):
        args = ", ".join(_render_expr_inline(a) for a in expr.args)
        return f"{expr.callee}({args})"
    if isinstance(expr, IRRaw):
        return expr.text
    return "/* ? */"


def _extract_intent(
    intent_spec: Node,
) -> Literal["in", "out", "inout"] | None:
    """Read an ``IntentSpec`` node's enum value.

    The dump-parse-tree NODE_ENUM macro renders ``IntentSpec::Intent``
    children with a kind like ``"Intent = In"`` / ``"Intent = Out"`` /
    ``"Intent = InOut"`` — we parse the right-hand side.
    """
    for child in intent_spec.children:
        if child.kind.startswith("Intent ="):
            value = child.kind.split("=", 1)[1].strip().lower()
            if value == "in":
                return "in"
            if value == "out":
                return "out"
            if value == "inout":
                return "inout"
    return None


# ---------------------------------------------------------------------------
# Execution part
# ---------------------------------------------------------------------------


def _lower_execution(exec_part: Node) -> list[IRStatement]:
    """Translate an ExecutionPart's Block into a flat list of statements."""
    body: list[IRStatement] = []
    for child in exec_part.walk():
        if child.kind == "Block":
            body.extend(_lower_block(child))
            break  # The first Block is the execution part's body.
    return body


def _lower_block(block: Node) -> list[IRStatement]:
    out: list[IRStatement] = []
    for construct in block.children:
        label = _leading_label(construct)
        if label is not None:
            out.append(IRLabel(label=label))
        stmt = _lower_construct(construct)
        if stmt is not None:
            out.append(stmt)
    return out


def _leading_label(construct: Node) -> int | None:
    """The statement label on a construct's leading statement, if any —
    a goto target like ``10 continue`` or ``100 if (...) then``."""
    target = _drill(
        construct, skip={"ExecutionPartConstruct", "ExecutableConstruct"}
    )
    if target is None:
        return None
    if target.kind == "Statement":
        # A FORMAT statement's label is a format reference, not a branch
        # target, so it must not become a structuring IRLabel.
        if target.find_first("FormatStmt") is not None:
            return None
        return target.label
    first_stmt = target.first_child("Statement")
    return first_stmt.label if first_stmt is not None else None


def _lower_construct(construct: Node) -> IRStatement | None:
    """Translate a single ``ExecutionPartConstruct``-level node."""
    # Drill through transparent wrappers down to the meaningful node.
    target = _drill(
        construct,
        skip={"ExecutionPartConstruct", "ExecutableConstruct"},
    )
    if target is None:
        return None

    if target.kind == "Statement":
        return _lower_action_statement(target)
    if target.kind == "IfConstruct":
        return _lower_if_construct(target)
    if target.kind == "DoConstruct":
        return _lower_do_construct(target)
    if target.kind == "CaseConstruct":
        return _lower_case_construct(target)
    if target.kind == "WhereConstruct":
        return _lower_where_construct(target)
    if target.kind == "AssociateConstruct":
        return _lower_associate_construct(target)
    if target.kind == "BlockConstruct":
        return _lower_block_construct(target)
    if target.kind == "ForallConstruct":
        return _lower_forall(target)
    return _unsupported(target, kind=target.kind)


def _lower_associate_construct(node: Node) -> IRStatement:
    """``associate (h => expr) ... end associate`` -> a scoped block of
    ``auto&& h = expr;`` bindings around the body."""
    bindings: list[tuple[str, IRExpr]] = []
    body: list[IRStatement] = []
    for child in node.children:
        if child.kind == "Statement":
            stmt = child.find_first("AssociateStmt")
            if stmt is not None:
                for assoc in stmt.find_all("Association"):
                    name = assoc.first_child("Name")
                    sel = assoc.find_first("Selector")
                    if name is None or not name.fortran or sel is None:
                        continue
                    # The selector's *direct* child is the Expr/Variable;
                    # a recursive search would wrongly grab a nested
                    # sub-expression (e.g. sqrt's argument).
                    expr = sel.first_child("Expr") or sel.first_child("Variable")
                    if expr is not None:
                        bindings.append(
                            (_safe_name(name.fortran), _lower_expression(expr))
                        )
        elif child.kind == "Block":
            body = _lower_block(child)
    return IRBlock(bindings=bindings, body=body)


def _lower_block_construct(node: Node) -> IRStatement:
    """``block ... end block`` -> a scoped block with local declarations."""
    locals_: list[IRLocal] = []
    body: list[IRStatement] = []
    for child in node.children:
        if child.kind == "BlockSpecificationPart":
            spec = child.first_child("SpecificationPart")
            if spec is not None:
                locals_ = _lower_specification(spec)
        elif child.kind == "Block":
            body = _lower_block(child)
    return IRBlock(locals=locals_, body=body)


def _lower_where_construct(node: Node) -> IRStatement:
    mask: IRExpr = IRRaw("true")
    where_body: list[IRStatement] = []
    elsewhere_body: list[IRStatement] | None = None
    for child in node.children:
        if child.kind == "Statement":
            wcs = child.find_first("WhereConstructStmt")
            if wcs is not None:
                e = wcs.find_first("Expr")
                if e is not None:
                    mask = _lower_expression(e)
        elif child.kind == "WhereBodyConstruct":
            asgn = _lower_where_body(child)
            if asgn is not None:
                where_body.append(asgn)
        elif child.kind in ("Elsewhere", "MaskedElsewhere"):
            elsewhere_body = []
            for sub in child.children:
                if sub.kind == "WhereBodyConstruct":
                    asgn = _lower_where_body(sub)
                    if asgn is not None:
                        elsewhere_body.append(asgn)
    return IRWhere(
        mask=mask, where_body=where_body, elsewhere_body=elsewhere_body
    )


def _lower_where_body(wbc: Node) -> IRStatement | None:
    asgn = wbc.find_first("AssignmentStmt")
    return _lower_assignment(asgn) if asgn is not None else None


def _lower_where_stmt(node: Node) -> IRStatement:
    mask: IRExpr = IRRaw("true")
    log = node.first_child("Logical")
    e = log.find_first("Expr") if log is not None else node.find_first("Expr")
    if e is not None:
        mask = _lower_expression(e)
    asgn = node.find_first("AssignmentStmt")
    body: list[IRStatement] = [_lower_assignment(asgn)] if asgn is not None else []
    return IRWhere(mask=mask, where_body=body, elsewhere_body=None)


def _lower_action_statement(stmt: Node) -> IRStatement | None:
    """A ``Statement`` wrapper around an ``ActionStmt``."""
    leading = list(stmt.leading_comments)
    trailing = list(stmt.trailing_comments)
    # FORMAT and DATA are non-executable: FORMAT is resolved by label
    # where referenced, and DATA is collected unit-wide and prepended as
    # initializers — so drop both where they appear.
    if (
        stmt.find_first("FormatStmt") is not None
        or stmt.find_first("DataStmt") is not None
    ):
        return IRComment(comments=[])
    # ENTRY is a standalone statement (not an ActionStmt); emit the
    # transient marker the body split keys off.
    entry = stmt.find_first("EntryStmt")
    if entry is not None:
        return _lower_entry_stmt(entry)
    action = stmt.find_first("ActionStmt")
    if action is None:
        return _unsupported(stmt, kind="Statement")
    inner = next(iter(action.children), None)
    if inner is None:
        return _unsupported(action, kind="ActionStmt")
    result = _lower_action_inner(inner)
    if result is None:
        return _unsupported(inner, kind=inner.kind, leading=leading)
    # IRComment (e.g. a lowered CONTINUE) carries comments differently;
    # only statements with the standard comment slots get them attached.
    if hasattr(result, "leading_comments"):
        result.leading_comments = leading
        result.trailing_comments = trailing
    return result


# Inner-statement kind -> lowering function.  Both the normal action
# statement and the single-line ``if (c) action`` form dispatch through
# here, so a new action statement only needs to be registered once.
def _lower_action_inner(inner: Node) -> IRStatement | None:
    match inner.kind:
        case "AssignmentStmt":
            return _lower_assignment(inner)
        case "PrintStmt":
            return _lower_print(inner)
        case "WriteStmt":
            return _lower_write(inner)
        case "ReadStmt":
            return _lower_read(inner)
        case "OpenStmt":
            return _lower_open(inner)
        case "CloseStmt":
            return _lower_close(inner)
        case "InquireStmt":
            return _lower_inquire(inner)
        case "BackspaceStmt":
            return _lower_file_position(inner, "backspace")
        case "RewindStmt":
            return _lower_file_position(inner, "rewind")
        case "EndfileStmt":
            return _lower_endfile(inner)
        case "StopStmt":
            return _lower_stop(inner)
        case "AllocateStmt":
            return _lower_allocate(inner)
        case "DeallocateStmt":
            return _lower_deallocate(inner)
        case "WhereStmt":
            return _lower_where_stmt(inner)
        case "PointerAssignmentStmt":
            return _lower_pointer_assignment(inner)
        case "NullifyStmt":
            return _lower_nullify(inner)
        case "CallStmt":
            return _lower_call(inner)
        case "ReturnStmt":
            return IRReturn()
        case "CycleStmt":
            return IRCycle()
        case "ExitStmt":
            return IRExit()
        case "IfStmt":
            return _lower_if_stmt(inner)
        case "ForallStmt":
            return _lower_forall(inner)
        case "GotoStmt":
            return _lower_goto(inner)
        case "ComputedGotoStmt":
            return _lower_computed_goto(inner)
        case "ArithmeticIfStmt":
            return _lower_arithmetic_if(inner)
        case "ContinueStmt":
            # CONTINUE is a no-op (often just a labeled loop terminator,
            # which the structured loop already absorbs).
            return IRComment(comments=[])
        case "EntryStmt":
            return _lower_entry_stmt(inner)
    return None


def _lower_entry_stmt(node: Node) -> IRStatement:
    """``ENTRY name(args)`` -> a transient marker; the body split turns
    each into a standalone subprogram (statements from here onward)."""
    name_node = node.first_child("Name")
    name = _safe_name(name_node.fortran) if name_node and name_node.fortran else ""
    args: list[str] = []
    for da in node.children:
        if da.kind == "DummyArg":
            nm = da.first_child("Name")
            if nm is not None and nm.fortran:
                args.append(_safe_name(nm.fortran))
    return IREntry(name=name, arg_names=tuple(args))


def _lower_if_stmt(node: Node) -> IRStatement:
    """Lower a single-statement ``if (cond) action`` (no ``then``).

    Modeled as an IRIf with one branch holding the single action,
    reusing the shared action dispatcher so every action kind works.
    """
    cond_expr = node.find_first("Expr")
    condition = _lower_expression(cond_expr) if cond_expr else IRRaw("true")
    body: list[IRStatement] = []
    unlabeled = node.find_first("UnlabeledStatement")
    if unlabeled is not None:
        action = unlabeled.find_first("ActionStmt")
        if action is not None:
            inner = next(iter(action.children), None)
            if inner is not None:
                lowered = _lower_action_inner(inner)
                body = [
                    lowered
                    if lowered is not None
                    else _unsupported(inner, kind=inner.kind)
                ]
    # ``if (c) goto N`` is kept as a single guarded goto so the
    # structuring pass can recognize the forward-skip / loop idioms.
    if len(body) == 1 and isinstance(body[0], IRGoto) and body[0].condition is None:
        return IRGoto(target=body[0].target, condition=condition)
    return IRIf(branches=[(condition, body)], else_body=None)


def _label_targets(node: Node) -> list[int]:
    out: list[int] = []
    for c in node.children_of_kind("uint64_t"):
        if c.fortran:
            try:
                out.append(int(c.fortran))
            except ValueError:
                pass
    return out


def _lower_goto(node: Node) -> IRStatement:
    labels = _label_targets(node)
    return IRGoto(target=labels[0]) if labels else IRComment(comments=[])


def _lower_computed_goto(node: Node) -> IRStatement:
    """``goto (l1, l2, ...), e`` -> guarded gotos on ``e == k`` (1-based)."""
    labels = _label_targets(node)
    sel = node.first_child("Scalar")
    sel_expr = None
    if sel is not None:
        e = sel.find_first("Expr")
        sel_expr = _lower_expression(e) if e is not None else None
    if sel_expr is None or not labels:
        return IRComment(comments=[])
    branches = [
        (
            IRBinaryOp(op="==", lhs=sel_expr, rhs=IRLiteral(cpp_text=str(idx))),
            [IRGoto(target=lbl)],
        )
        for idx, lbl in enumerate(labels, start=1)
    ]
    return IRIf(branches=branches, else_body=None)


def _lower_arithmetic_if(node: Node) -> IRStatement:
    """``if (e) ln, lz, lp`` -> goto by the sign of ``e``."""
    e = node.first_child("Expr")
    val = _lower_expression(e) if e is not None else IRRaw("0")
    labels = _label_targets(node)
    if len(labels) < 3:
        return IRComment(comments=[])
    neg, zero, pos = labels[0], labels[1], labels[2]
    branches = [
        (IRBinaryOp(op="<", lhs=val, rhs=IRLiteral(cpp_text="0")),
         [IRGoto(target=neg)]),
        (IRBinaryOp(op="==", lhs=val, rhs=IRLiteral(cpp_text="0")),
         [IRGoto(target=zero)]),
    ]
    return IRIf(branches=branches, else_body=[IRGoto(target=pos)])


# Array intrinsics that take a whole array and return a scalar (or
# array) — their array arguments must NOT be element-indexed when a
# whole-array assignment is expanded into a loop.
_NON_ELEMENTAL: frozenset[str] = frozenset(
    {
        "ftn::sum", "ftn::product", "ftn::maxval",
        "ftn::minval", "ftn::count", "ftn::any",
        "ftn::all", "ftn::dot_product", "ftn::size",
        "ftn::lbound", "ftn::ubound",
        "ftn::matmul", "ftn::transpose",
        "ftn::maxloc", "ftn::minloc",
        "ftn::pack", "ftn::cshift",
    }
)

# Intrinsics that return a whole array.  ``c = matmul(a, b)`` must NOT
# be expanded into an element loop (you can't index the call result);
# it stays a move-assignment of the returned Array.
_ARRAY_RETURNING: frozenset[str] = frozenset(
    {"ftn::matmul", "ftn::transpose", "ftn::reshape",
     "ftn::pack", "ftn::cshift", "ftn::eoshift",
     "ftn::spread"}
)


def _expand_array_assignments(
    sub: IRSubprogram, resolved_arrays: dict[str, IRType] | None = None
) -> None:
    """Expand whole-array assignments (``a = b + c``) into explicit
    element loops, indexing the array operands and leaving scalars and
    whole-array (reduction) calls alone.

    Generated code reads like a hand-written loop nest and allocates no
    temporaries.  Array sections (``a(1:5) = ...``) are not handled
    here yet.
    """
    arrays = {loc.name: loc.type for loc in sub.locals if loc.type.is_array}
    # WHERE may target host-associated / module / COMMON arrays that aren't
    # locals (e.g. NRLMSIS2's module ``specflag``).  Give *only* the WHERE
    # handlers a view augmented with flang's resolved array shapes, so those
    # cases stop erroring -- the whole-array / section paths below keep using
    # the locals-only view, leaving every currently-working translation
    # byte-for-byte unchanged.
    where_arrays = {**(resolved_arrays or {}), **arrays}
    if not arrays and not where_arrays:
        return
    array_names = set(arrays)
    where_array_names = set(where_arrays)
    counter = [0]

    def expand(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRWhere):
            return _where_loop(
                stmt, where_arrays, where_array_names, counter
            )
        if not isinstance(stmt, IRAssignment):
            return stmt
        tgt = stmt.target
        rhs_has_section = _contains_section(stmt.value)
        if (
            isinstance(tgt, IRName)
            and tgt.name in arrays
            and rhs_has_section
            and arrays[tgt.name].array_rank >= 2
        ):
            # ``a = b(:,:,k)`` — a rank>=2 *whole-array* target assigned an
            # array-valued (section) expression.  The element-loop expander
            # emits a single loop that indexes the target with one
            # subscript, wrong for a multidimensional array; assign as a
            # whole instead and let the runtime operator= copy elements.
            return stmt
        if isinstance(tgt, IRSection) or rhs_has_section:
            # ``a(lo:hi) = matmul(...)`` / ``= [v1, v2]`` — the RHS is a
            # whole array-valued result with no section to index
            # elementwise, so assign the section as a whole
            # (ArrayRef::operator= copies the elements) rather than
            # scattering the array result into a scalar slot.
            if (
                isinstance(tgt, IRSection)
                and not rhs_has_section
                and (
                    isinstance(stmt.value, IRArrayConstructor)
                    or (
                        isinstance(stmt.value, IRFunctionCall)
                        and stmt.value.callee in _ARRAY_RETURNING
                    )
                )
            ):
                return stmt
            loop = _section_assignment_loop(stmt, arrays, array_names, counter)
            return loop if loop is not None else stmt
        if isinstance(tgt, IRName) and tgt.name in arrays:
            # ``a = [(expr, i=lo,hi)]`` -> a fill loop.
            if isinstance(stmt.value, IRImpliedDo):
                return _implied_do_fill_loop(tgt.name, stmt.value, stmt)
            # An array-returning intrinsic (matmul/transpose) or a plain
            # array constructor stays a whole-array move-assignment.
            if isinstance(stmt.value, IRArrayConstructor) or (
                isinstance(stmt.value, IRFunctionCall)
                and stmt.value.callee in _ARRAY_RETURNING
            ):
                return stmt
            return _array_assignment_loop(
                stmt, arrays[tgt.name], array_names, counter
            )
        return stmt

    sub.body = [map_statement(s, on_stmt=expand) for s in sub.body]


def _where_loop(
    where: IRWhere,
    arrays: dict[str, IRType],
    array_names: set[str],
    counter: list[int],
) -> IRStatement:
    """Expand a WHERE into a masked element loop nest.

    Shape (rank, bounds) comes from the first where-body assignment's
    target array.  Each body assignment and the mask are indexed at the
    loop variables; the body runs under ``if (mask) ... else ...``.
    """
    target_array = None
    for asgn in where.where_body:
        if isinstance(asgn, IRAssignment) and isinstance(asgn.target, IRName):
            if asgn.target.name in arrays:
                target_array = asgn.target.name
                break
    if target_array is None:
        # No whole-array name target -- the bodies assign to array *sections*
        # (``where (.not. swg(0:m)) p(0:m) = 0``).  Fall back to the rank-1
        # section model: one ``_k`` position counter, every section / array
        # indexed at ``_k`` (mask and RHS included).
        section_loop = _where_section_loop(where, array_names, counter)
        if section_loop is not None:
            return section_loop
        return _unsupported_stmt("WHERE with no whole-array target")

    atype = arrays[target_array]
    rank = max(atype.array_rank, 1)
    idx_vars: list[str] = []
    for _ in range(rank):
        counter[0] += 1
        idx_vars.append(f"_i{counter[0]}")
    idx_args = tuple(IRName(name=v, fortran=v) for v in idx_vars)

    def index_body(body: list[IRStatement]) -> list[IRStatement]:
        out: list[IRStatement] = []
        for asgn in body:
            if not (
                isinstance(asgn, IRAssignment)
                and isinstance(asgn.target, IRName)
                and asgn.target.name in array_names
            ):
                continue
            out.append(
                IRAssignment(
                    target=IRFunctionCall(
                        callee=asgn.target.name, args=idx_args
                    ),
                    value=_index_array_expr(asgn.value, idx_args, array_names),
                    leading_comments=asgn.leading_comments,
                    trailing_comments=asgn.trailing_comments,
                )
            )
        return out

    mask_i = _index_array_expr(where.mask, idx_args, array_names)
    then_body = index_body(where.where_body)
    else_body = (
        index_body(where.elsewhere_body)
        if where.elsewhere_body is not None
        else None
    )
    inner: list[IRStatement] = [
        IRIf(branches=[(mask_i, then_body)], else_body=else_body)
    ]
    loop: list[IRStatement] = inner
    for k in range(rank):
        var = idx_vars[k]
        loop = [
            IRDo(
                var=var,
                lower=IRRaw(f"{target_array}.lbound({k + 1})"),
                upper=IRRaw(f"{target_array}.ubound({k + 1})"),
                step=None,
                body=loop,
                declare=True,
            )
        ]
    return loop[0]


def _where_section_loop(
    where: IRWhere,
    array_names: set[str],
    counter: list[int],
) -> IRStatement | None:
    """Expand a WHERE whose bodies target array *sections* into a single
    rank-1 ``_k`` position loop with a masked ``if`` (the section analogue of
    :func:`_where_loop`).  Returns None if no body assignment is a section /
    whole-array target we can size (then the caller fails loudly).

    ``where (.not. swg(0:m)) p(0:m) = 0`` becomes
    ``for (_k=0; _k<=(m-0+1)-1; ++_k) if (!swg(0+_k)) p(0+_k) = 0``.
    The mask, every RHS section, and the target all index at the same ``_k``,
    so differing section lower bounds line up the way Fortran intends."""
    # Size the loop from the first section/array target we can measure.
    count: IRExpr | None = None
    for asgn in where.where_body:
        if isinstance(asgn, IRAssignment) and isinstance(
            asgn.target, (IRSection, IRName)
        ):
            counter[0] += 1
            kname = f"_k{counter[0]}"
            kvar: IRExpr = IRName(name=kname, fortran=kname)
            _, count = _section_kth(asgn.target, kvar, array_names)
            if count is not None:
                break
    if count is None:
        return None

    def index_body(body: list[IRStatement]) -> list[IRStatement]:
        out: list[IRStatement] = []
        for asgn in body:
            if not isinstance(asgn, IRAssignment):
                continue
            target_access, _ = _section_kth(asgn.target, kvar, array_names)
            if target_access is None:
                continue
            out.append(
                IRAssignment(
                    target=target_access,
                    value=_section_index_rhs(asgn.value, kvar, array_names),
                    leading_comments=asgn.leading_comments,
                    trailing_comments=asgn.trailing_comments,
                )
            )
        return out

    mask_k = _section_index_rhs(where.mask, kvar, array_names)
    then_body = index_body(where.where_body)
    else_body = (
        index_body(where.elsewhere_body)
        if where.elsewhere_body is not None
        else None
    )
    inner: list[IRStatement] = [
        IRIf(branches=[(mask_k, then_body)], else_body=else_body)
    ]
    return IRDo(
        var=kname,
        lower=IRLiteral(cpp_text="0"),
        upper=IRBinaryOp(op="-", lhs=count, rhs=IRLiteral(cpp_text="1")),
        step=None,
        body=inner,
        declare=True,
    )


def _unsupported_stmt(note: str) -> NoReturn:
    raise ConversionError("WHERE", note=note)


def _implied_do_fill_loop(
    target: str, impl: IRImpliedDo, stmt: IRAssignment
) -> IRStatement:
    """``a = [(value, i=lo,hi[,step])]`` -> a loop filling a in order.

    Element position is ``(i - lo)`` (or ``(i - lo)/step``); the
    destination index is ``a.lbound(1) + position``.
    """
    value = impl.items[0] if impl.items else IRRaw("0")
    pos = IRBinaryOp(op="-", lhs=IRName(name=impl.var, fortran=impl.var),
                     rhs=impl.lower)
    if impl.step is not None:
        pos = IRBinaryOp(op="/", lhs=pos, rhs=impl.step)
    dest = IRBinaryOp(op="+", lhs=IRRaw(f"{target}.lbound(1)"), rhs=pos)
    body: list[IRStatement] = [
        IRAssignment(
            target=IRFunctionCall(callee=target, args=(dest,)),
            value=value,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    ]
    return IRDo(
        var=impl.var,
        lower=impl.lower,
        upper=impl.upper,
        step=impl.step,
        body=body,
        declare=True,
    )


def _contains_section(expr: IRExpr) -> bool:
    found = [False]

    def note(e: IRExpr) -> IRExpr:
        if isinstance(e, IRSection):
            found[0] = True
        return e

    from .transform import map_expr

    map_expr(expr, note)
    return found[0]


def _section_assignment_loop(
    stmt: IRAssignment,
    arrays: dict[str, IRType],
    array_names: set[str],
    counter: list[int],
) -> IRStatement | None:
    """Expand a rank-1 section assignment into an explicit element loop.

    Iterates an element-position counter ``_k`` (0-based) and maps each
    array operand / section to its own index at ``_k`` — which is what
    makes ``a(1:5) = b(2:6)`` correct even though the index ranges
    differ.  Returns None for unsupported shapes (rank >= 2 sections).
    """
    counter[0] += 1
    kname = f"_k{counter[0]}"
    kvar = IRName(name=kname, fortran=kname)

    target_access, count = _section_kth(stmt.target, kvar, array_names)
    if target_access is None or count is None:
        return None
    rhs = _section_index_rhs(stmt.value, kvar, array_names)
    body: list[IRStatement] = [
        IRAssignment(
            target=target_access,
            value=rhs,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    ]
    upper = IRBinaryOp(op="-", lhs=count, rhs=IRLiteral(cpp_text="1"))
    return IRDo(
        var=kname,
        lower=IRLiteral(cpp_text="0"),
        upper=upper,
        step=None,
        body=body,
        declare=True,
    )


def _section_kth(
    operand: IRExpr, kvar: IRExpr, array_names: set[str]
) -> tuple[IRExpr | None, IRExpr | None]:
    """Return (element-access-at-k, element-count) for a rank-1 section
    or whole array operand; (None, None) if unsupported."""
    if isinstance(operand, IRSection):
        triplet_pos = [
            i for i, s in enumerate(operand.subscripts)
            if isinstance(s, IRTriplet)
        ]
        if len(triplet_pos) != 1:
            return None, None  # only rank-1 sections for now
        d = triplet_pos[0]
        trip = operand.subscripts[d]
        a = operand.array
        lower = trip.lower if trip.lower is not None else IRRaw(f"{a}.lbound({d + 1})")
        upper = trip.upper if trip.upper is not None else IRRaw(f"{a}.ubound({d + 1})")
        if trip.stride is None:
            idx_d: IRExpr = IRBinaryOp(op="+", lhs=lower, rhs=kvar)
            count: IRExpr = IRBinaryOp(
                op="+",
                lhs=IRBinaryOp(op="-", lhs=upper, rhs=lower),
                rhs=IRLiteral(cpp_text="1"),
            )
        else:
            st = trip.stride
            idx_d = IRBinaryOp(
                op="+", lhs=lower, rhs=IRBinaryOp(op="*", lhs=kvar, rhs=st)
            )
            count = IRBinaryOp(
                op="+",
                lhs=IRBinaryOp(
                    op="/",
                    # Parenthesize the synthesized numerator: emit isn't
                    # precedence-aware, so ``upper - lower / st`` would bind
                    # the division to ``lower`` and overrun the section.
                    lhs=IRUnaryOp(
                        op="()", operand=IRBinaryOp(op="-", lhs=upper, rhs=lower)
                    ),
                    rhs=st,
                ),
                rhs=IRLiteral(cpp_text="1"),
            )
        args = tuple(
            idx_d if i == d else s for i, s in enumerate(operand.subscripts)
        )
        return IRFunctionCall(callee=a, args=args), count
    if isinstance(operand, IRName) and operand.name in array_names:
        a = operand.name
        access = IRFunctionCall(
            callee=a,
            args=(IRBinaryOp(op="+", lhs=IRRaw(f"{a}.lbound(1)"), rhs=kvar),),
        )
        return access, IRRaw(f"{a}.extent(1)")
    return None, None


def _section_index_rhs(
    expr: IRExpr, kvar: IRExpr, array_names: set[str]
) -> IRExpr:
    """Rewrite an RHS for the section (position-k) model."""
    if isinstance(expr, IRSection):
        access, _ = _section_kth(expr, kvar, array_names)
        return access if access is not None else expr
    if isinstance(expr, IRName) and expr.name in array_names:
        access, _ = _section_kth(expr, kvar, array_names)
        return access if access is not None else expr
    if isinstance(expr, IRBinaryOp):
        return IRBinaryOp(
            op=expr.op,
            lhs=_section_index_rhs(expr.lhs, kvar, array_names),
            rhs=_section_index_rhs(expr.rhs, kvar, array_names),
        )
    if isinstance(expr, IRUnaryOp):
        return IRUnaryOp(
            op=expr.op, operand=_section_index_rhs(expr.operand, kvar, array_names)
        )
    if isinstance(expr, IRCast):
        return IRCast(
            cpp_type=expr.cpp_type,
            operand=_section_index_rhs(expr.operand, kvar, array_names),
        )
    if isinstance(expr, IRFunctionCall):
        if expr.callee in _NON_ELEMENTAL:
            return expr
        return IRFunctionCall(
            callee=expr.callee,
            args=tuple(
                _section_index_rhs(a, kvar, array_names) for a in expr.args
            ),
        )
    return expr


def _array_assignment_loop(
    stmt: IRAssignment,
    atype: IRType,
    array_names: set[str],
    counter: list[int],
) -> IRStatement:
    rank = max(atype.array_rank, 1)
    tname = stmt.target.name  # type: ignore[union-attr]
    idx_vars: list[str] = []
    for _ in range(rank):
        counter[0] += 1
        idx_vars.append(f"_i{counter[0]}")
    idx_args = tuple(IRName(name=v, fortran=v) for v in idx_vars)

    rhs = _index_array_expr(stmt.value, idx_args, array_names)
    body: list[IRStatement] = [
        IRAssignment(
            target=IRFunctionCall(callee=tname, args=idx_args),
            value=rhs,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    ]
    # Nest loops with dim 1 innermost (column-major), dim ``rank``
    # outermost.  Bounds come from the target array's runtime extents.
    loop: list[IRStatement] = body
    for k in range(rank):
        var = idx_vars[k]
        loop = [
            IRDo(
                var=var,
                lower=IRRaw(f"{tname}.lbound({k + 1})"),
                upper=IRRaw(f"{tname}.ubound({k + 1})"),
                step=None,
                body=loop,
                declare=True,
            )
        ]
    return loop[0]


def _index_array_expr(
    expr: IRExpr, idx: tuple[IRExpr, ...], array_names: set[str]
) -> IRExpr:
    """Rewrite an elementwise RHS: bare array names become indexed
    accesses; operators and elemental calls recurse; whole-array
    (reduction) calls are left untouched."""
    if isinstance(expr, IRName):
        if expr.name in array_names:
            return IRFunctionCall(callee=expr.name, args=idx)
        return expr
    if isinstance(expr, IRBinaryOp):
        return IRBinaryOp(
            op=expr.op,
            lhs=_index_array_expr(expr.lhs, idx, array_names),
            rhs=_index_array_expr(expr.rhs, idx, array_names),
        )
    if isinstance(expr, IRUnaryOp):
        return IRUnaryOp(
            op=expr.op,
            operand=_index_array_expr(expr.operand, idx, array_names),
        )
    if isinstance(expr, IRCast):
        return IRCast(
            cpp_type=expr.cpp_type,
            operand=_index_array_expr(expr.operand, idx, array_names),
        )
    if isinstance(expr, IRFunctionCall):
        if expr.callee in _NON_ELEMENTAL:
            return expr  # whole-array argument; do not index
        # Elemental intrinsic (std::sqrt, ...) or an array element
        # access whose callee is an array name: recurse into args.
        return IRFunctionCall(
            callee=expr.callee,
            args=tuple(
                _index_array_expr(a, idx, array_names) for a in expr.args
            ),
        )
    return expr


def _lower_assignment(node: Node) -> IRStatement:
    """Lower a Fortran assignment ``v = e`` to an :class:`IRAssignment`,
    expanding it into a loop when ``v`` is a whole array or section and
    the RHS is array-valued.  Plain scalar assignments map to the obvious
    target-value pair."""
    target = node.first_child("Variable") or node.first_child("Designator")
    value = node.first_child("Expr")
    tgt = _lower_expression(target) if target else IRRaw("/* ? */")
    val = _lower_expression(value) if value else IRRaw("/* ? */")
    # Vector subscript on the left: ``a([i, j, k]) = rhs`` assigns ``rhs``
    # to each indexed element.  Expand to one assignment per index (the
    # subscript constructor's elements are constants here).
    if (
        isinstance(tgt, IRFunctionCall)
        and len(tgt.args) == 1
        and isinstance(tgt.args[0], IRArrayConstructor)
    ):
        return IRBlock(
            bindings=[],
            body=[
                IRAssignment(
                    target=IRFunctionCall(callee=tgt.callee, args=(idx,)),
                    value=val,
                )
                for idx in tgt.args[0].elements
            ],
        )
    return IRAssignment(target=tgt, value=val)


def _lower_print(node: Node) -> IRPrint:
    fmt_kind, fmt_payload = _classify_format(node)
    expand = fmt_kind == "const"
    return IRPrint(
        items=_lower_io_items(
            node, "OutputItem", "OutputImpliedDo", expand_whole_arrays=expand
        ),
        format=fmt_payload if fmt_kind == "const" else None,
        format_expr=fmt_payload if fmt_kind == "runtime" else None,
    )


def _lower_io_items(
    node: Node, item_kind: str, implied_kind: str,
    *, expand_whole_arrays: bool = False,
) -> list[IRExpr]:
    """Lower a print/read item list, handling implied-do items.

    When ``expand_whole_arrays`` is true, a whole-array reference in the
    item list (``DL`` for a rank-1 array) is expanded into one item per
    element so a labeled FORMAT cycles correctly across the array's
    elements.  Off by default — unformatted record I/O (which writes the
    whole array as one contiguous blob) and list-directed I/O (whose
    runtime ``<<`` / ``>>`` overloads iterate elements themselves) want
    the whole-array item preserved."""
    items: list[IRExpr] = []
    for sub in node.children:
        if sub.kind == item_kind:
            # An item may wrap an implied-do; check that before falling
            # back to a recursive Expr search (which would wrongly pick
            # up the implied-do's inner element).
            impl = sub.first_child(implied_kind)
            if impl is not None:
                items.append(
                    _lower_io_implied_do(impl, item_kind, implied_kind)
                )
                continue
            # Direct child only: a recursive Expr search would pick up
            # an array subscript (the ``i`` in ``a(i)``).
            expr = sub.first_child("Expr") or sub.first_child("Variable")
            if expr is not None:
                expanded = (
                    _expand_whole_array_io_item(expr)
                    if expand_whole_arrays else None
                )
                if expanded is not None:
                    items.extend(expanded)
                else:
                    items.append(_lower_expression(expr))
        elif sub.kind == implied_kind:
            items.append(_lower_io_implied_do(sub, item_kind, implied_kind))
    return items


def _expand_whole_array_io_item(expr: Node) -> list[IRExpr] | None:
    """If *expr* is a whole-array reference with a compile-time-known
    shape, return one IRExpr per element (Fortran array-element order:
    leftmost subscript varies fastest).  Returns ``None`` otherwise."""
    if expr.kind == "Expr":
        if expr.first_child("LiteralConstant") is not None:
            return None
        inner = expr.find_first("Designator") or expr.first_child("Name")
    else:
        inner = expr
    if inner is None:
        return None
    name = inner if inner.kind == "Name" else None
    if name is None and inner.kind == "Designator":
        # A bare-Name designator: no DataRef components, no subscripts.
        dref = inner.first_child("DataRef")
        if dref is not None and len(dref.children) == 1:
            cand = dref.children[0]
            if cand.kind == "Name":
                name = cand
        else:
            cand = inner.first_child("Name")
            if cand is not None:
                name = cand
    if name is None:
        return None
    rank = name.rank
    if rank is None or rank < 1:
        return None
    shape = name.shape
    if shape is None or len(shape) != rank:
        return None
    base = _lower_expression(expr)
    if not isinstance(base, IRName):
        return None

    def cm_indices(dims_left: list[tuple[int, int]]) -> list[tuple[int, ...]]:
        if not dims_left:
            return [()]
        lo, hi = dims_left[0]
        rest = cm_indices(dims_left[1:])
        result: list[tuple[int, ...]] = []
        for outer in rest:
            for i in range(lo, hi + 1):
                result.append((i,) + outer)
        return result

    out: list[IRExpr] = []
    for idx in cm_indices(list(shape)):
        args = tuple(IRLiteral(cpp_text=str(i)) for i in idx)
        out.append(IRFunctionCall(callee=base.name, args=args))
    return out


def _lower_io_implied_do(
    node: Node, item_kind: str, implied_kind: str
) -> IRExpr:
    lb = node.find_first("LoopBounds")
    var, lo, hi, step = _lower_loop_bounds(lb)
    inner = _lower_io_items(node, item_kind, implied_kind)
    return IRImpliedDo(
        var=var, lower=lo, upper=hi, step=step, items=tuple(inner)
    )


def _lower_write(
    node: Node,
) -> "IRPrint | IRDirectWrite | IRUnformattedDirectWrite":
    """Lower ``write(unit, fmt[, REC=n]) items``.

    The unit selects the stream: ``*`` / ``6`` -> std::cout, ``0`` ->
    std::cerr.  ``REC=`` makes it a direct-access record write -- with a
    FORMAT it lowers to :class:`IRDirectWrite` (formatted text record);
    without a FORMAT it lowers to :class:`IRUnformattedDirectWrite`
    (raw-byte record).  Other (file) units write through the units table.
    """
    io_unit = _find_io_unit(node)
    fmt_kind, fmt_payload = _classify_format(node)
    rec_expr = _extract_rec(node)
    # ``_lower_io_items`` already handles both plain items and
    # OutputImpliedDo wrappers; using it here keeps the read- and
    # write-sides symmetric and lets implied-do items reach IR for
    # unformatted-direct write expansion.
    if rec_expr is not None:
        unit_text = _unit_text(io_unit)
        if unit_text is None:
            raise ConversionError(
                "direct-access WRITE",
                note="unsupported unit for a REC= write",
                source=node.source.text if node.source else "",
            )
        if fmt_kind == "runtime":
            raise ConversionError(
                "direct-access WRITE",
                note="runtime (non-constant) FORMAT with REC= unsupported",
                source=node.source.text if node.source else "",
            )
        fmt_str = fmt_payload if fmt_kind == "const" else None
        if fmt_str is None:
            # Unformatted record: keep whole-array items intact so
            # ``append_bytes(_wrec, drec)`` writes them as one contiguous
            # blob.
            items = _lower_io_items(node, "OutputItem", "OutputImpliedDo")
            return IRUnformattedDirectWrite(
                unit_text=unit_text, rec=rec_expr, items=items
            )
        items = _lower_io_items(
            node, "OutputItem", "OutputImpliedDo", expand_whole_arrays=True
        )
        return IRDirectWrite(
            unit_text=unit_text, rec=rec_expr, items=items, format=fmt_str
        )
    # Sequential write: only the formatted case needs whole-array
    # expansion (so format cycling counts each element as a separate
    # item).  List-directed writes stream the whole array via
    # ``operator<<``.
    expand = fmt_kind == "const"
    items = _lower_io_items(
        node, "OutputItem", "OutputImpliedDo", expand_whole_arrays=expand,
    )
    internal = _internal_file_unit(io_unit)
    stream = _stream_for_unit(io_unit)
    return IRPrint(
        items=items,
        stream=stream,
        format=fmt_payload if fmt_kind == "const" else None,
        format_expr=fmt_payload if fmt_kind == "runtime" else None,
        internal_unit=internal,
    )


def _lower_read(
    node: Node,
) -> "IRRead | IRDirectRead | IRUnformattedDirectRead":
    """Lower ``read *, items`` / ``read(unit, fmt[, REC=n]) items``.

    ``REC=`` selects direct-access record I/O: with a parseable FORMAT,
    :class:`IRDirectRead` slices fixed-width fields from a text record;
    without a FORMAT, :class:`IRUnformattedDirectRead` deserializes the
    items from a raw-byte record.  Otherwise (no REC=) this is
    list-directed input -- a ``>>`` chain (``*`` / ``5`` -> std::cin).
    """
    rec_expr = _extract_rec(node)
    fmt_str = _extract_format(node)
    io_unit = _find_io_unit(node)
    if rec_expr is not None:
        unit_text = _unit_text(io_unit)
        if unit_text is None:
            raise ConversionError(
                "direct-access READ",
                note="unsupported unit for a REC= read",
                source=node.source.text if node.source else "",
            )
        if fmt_str is None:
            # Unformatted direct read: deserialize each item from raw bytes
            # in declaration order.  Implied-do items lower to IRImpliedDo
            # nodes the emitter unrolls as runtime for-loops calling
            # ``take_bytes`` per element.
            items = _lower_io_items(node, "InputItem", "InputImpliedDo")
            return IRUnformattedDirectRead(
                unit_text=unit_text, rec=rec_expr, items=items
            )
        records = _build_direct_read_fields(node, fmt_str)
        if records is None or len(records) != 1:
            raise ConversionError(
                "direct-access READ",
                note="unsupported FORMAT for a REC= read"
                " (multi-record cycling has no record number to step)",
                source=node.source.text if node.source else "",
            )
        return IRDirectRead(unit_text=unit_text, rec=rec_expr, fields=records[0])
    items = _lower_io_items(node, "InputItem", "InputImpliedDo")
    internal = _internal_file_unit(io_unit)
    stream = _input_stream_for_unit(io_unit)
    end_label, err_label, iostat_target = _extract_io_status_specs(node)
    # A sequential ``READ(unit, format)`` with a compile-time format spec
    # that maps to fixed-width column slices: pre-resolve fields so the
    # emitter slices ``getline``'d bytes by offset rather than ``>>``
    # (which space-tokenizes -- wrong for files like apf107.dat whose
    # records are column-packed, e.g. ``-11257.0262.5241.9``).
    fmt_kind, fmt_payload = _classify_format(node)
    fields = None
    unit_text = None
    whole_line = False
    if (
        fmt_kind == "const"
        and isinstance(fmt_payload, str)
        and internal is None
    ):
        ut = _unit_text(io_unit)
        # Only file units route through ``_units.in(<n>)``; stdin (``*``,
        # ``5``) stays a plain ``>>`` chain (interactive / piped tests).
        if ut is not None and ut not in ("5", "*"):
            if _is_whole_line_a_format(fmt_payload):
                # ``READ(unit,'(A)') line`` reads the whole record into the
                # character item -- a getline, not a token read.
                whole_line = True
                unit_text = ut
            else:
                built = _build_direct_read_fields(node, fmt_payload)
                if built:
                    fields = built
                    unit_text = ut
    return IRRead(
        items=items,
        stream=stream,
        internal_unit=internal,
        unit_text=unit_text,
        fields=fields,
        whole_line=whole_line,
        end_label=end_label,
        err_label=err_label,
        iostat_target=iostat_target,
    )


def _is_whole_line_a_format(fmt_str: str) -> bool:
    """True when a FORMAT's only data descriptor(s) are ``A`` (no width) --
    i.e. ``(A)``, ``(1X,A)`` -- so each character item reads a whole record
    (blank-padded / truncated to its length).  A widthless ``A`` consumes
    the remainder of the record; mixing ``A`` with numeric descriptors is
    left to the field-slicing / list-directed paths and returns False."""
    from .format import parse_format, FormatParseError

    try:
        actions = parse_format(fmt_str)
    except FormatParseError:
        return False
    data = [a for a in actions if a.kind == "data"]
    if not data:
        return False
    for a in data:
        # Only bare ``A`` (no explicit width) means "whole remaining record".
        if a.letter.upper() != "A" or a.width:
            return False
    return True


def _extract_io_status_specs(
    node: Node,
) -> tuple[int | None, int | None, "IRExpr | None"]:
    """Pull ``END=label``, ``ERR=label`` and ``IOSTAT=var`` out of a
    READ/WRITE's IoControlSpec children.  Returns ``(end_label, err_label,
    iostat_target)``; any unset spec is ``None``."""
    end_label: int | None = None
    err_label: int | None = None
    iostat_target: "IRExpr | None" = None
    for spec in node.children_of_kind("IoControlSpec"):
        end = spec.first_child("EndLabel")
        if end is not None:
            lbl = end.first_child("uint64_t")
            if lbl is not None and lbl.fortran:
                try:
                    end_label = int(lbl.fortran)
                except ValueError:
                    pass
        err = spec.first_child("ErrLabel")
        if err is not None:
            lbl = err.first_child("uint64_t")
            if lbl is not None and lbl.fortran:
                try:
                    err_label = int(lbl.fortran)
                except ValueError:
                    pass
        iost = spec.first_child("IoStat") or spec.first_child("StatVariable")
        if iost is not None:
            # The variable is nested (StatVariable -> Scalar -> Integer ->
            # Variable -> Designator), so search rather than taking a direct
            # child -- otherwise IOSTAT= is silently dropped and the status
            # variable is never assigned (every EOF-checking read loops).
            iv = (
                iost.find_first("Variable")
                or iost.find_first("Designator")
                or iost.first_child("Expr")
            )
            if iv is not None:
                iostat_target = _lower_expression(iv)
    return end_label, err_label, iostat_target


def _extract_rec(node: Node) -> IRExpr | None:
    """``READ(unit, fmt, REC=n) ...`` -> lowered ``n`` (else ``None``)."""
    for spec in node.children_of_kind("IoControlSpec"):
        if spec.first_child("Rec") is not None:
            e = spec.find_first("Expr")
            if e is not None:
                return _lower_expression(e)
    return None


def _find_io_unit(node: Node) -> Node | None:
    """Locate the IoUnit on a READ/WRITE.  The positional form puts it
    as a direct child of the statement; the keyword form (``READ(UNIT =
    LUN, ...)`` -- common in SPICE) buries it inside an
    ``IoControlSpec``.  Find either."""
    direct = node.first_child("IoUnit")
    if direct is not None:
        return direct
    for spec in node.children_of_kind("IoControlSpec"):
        nested = spec.first_child("IoUnit")
        if nested is not None:
            return nested
    return None


def _build_direct_read_fields(
    node: Node, fmt_str: str
) -> list[list[tuple[IRExpr, str, int, int, int]]] | None:
    """Pair the parsed FORMAT with the InputItem list, returning a list
    of records where each record is a list of
    ``(target, kind, offset, width, decimals)`` tuples, or ``None`` when
    the format has descriptors we don't slice (character / unsupported —
    caller then falls back to list-directed).

    When the item list exceeds the format's data-descriptor count the
    format cycles to a new record (Fortran ``(1X,5E13.6)`` reading 1464
    items reads 293 lines).  Whole-array items expand element-by-element
    across the format's repeat counts (``8I3`` over an ``INTEGER(8)``
    array)."""
    from .format import parse_format, FormatParseError

    try:
        actions = parse_format(fmt_str)
    except FormatParseError:
        return None
    targets: list[IRExpr] = []
    for sub in node.children:
        if sub.kind != "InputItem":
            continue
        expr = sub.first_child("Expr") or sub.first_child("Variable")
        if expr is None:
            continue
        lowered = _lower_expression(expr)
        targets.extend(_expand_array_target(lowered, expr))
    # Pre-scan: a format with only character/unsupported descriptors fails
    # the whole resolution.  Detect supported descriptors only.
    for a in actions:
        if a.kind == "data":
            letter = a.letter.upper()
            if letter not in ("I", "F", "E", "D", "G", "X"):
                return None
    records: list[list[tuple[IRExpr, str, int, int, int]]] = []
    current: list[tuple[IRExpr, str, int, int, int]] = []
    pos = 0
    ti = 0

    def flush_record() -> None:
        nonlocal current, pos
        records.append(current)
        current = []
        pos = 0

    while ti < len(targets):
        for a in actions:
            if ti >= len(targets):
                break
            if a.kind == "literal":
                pos += len(a.text)
                continue
            if a.kind == "space":
                pos += a.count
                continue
            if a.kind == "newline":
                flush_record()
                continue
            if a.kind != "data":
                return None
            width = a.width or 0
            letter = a.letter.upper()
            if letter == "X":
                pos += width
                continue
            if letter == "I":
                kind = "int"
            elif letter in ("F", "E", "D", "G"):
                kind = "real"
            else:
                return None
            current.append((targets[ti], kind, pos, width, a.decimals or 0))
            ti += 1
            pos += width
        # End of one format cycle -- flush a record so cycling reads the
        # next line for the next batch of items.
        if ti < len(targets):
            flush_record()
    if current:
        records.append(current)
    if ti != len(targets):
        return None
    return records


def _expand_array_target(lowered: IRExpr, expr_node: Node) -> list[IRExpr]:
    """A whole-array input item reads one value per element; expand ``a``
    into ``a(1) ... a(N)`` (or ``a(i,j)`` for rank-N) so each maps 1:1 to
    a format descriptor.  Iteration order is Fortran column-major
    (leftmost subscript varies fastest).  Returns ``[lowered]`` for
    scalars or arrays of unknown size."""
    name = expr_node.find_first("Name")
    if name is None or not name.sym_type or name.rank is None or name.rank < 1:
        return [lowered]
    shape = name.shape
    if shape is None or len(shape) != name.rank:
        return [lowered]
    if not isinstance(lowered, IRName):
        return [lowered]
    # Column-major: leftmost index varies fastest.
    def cm_indices(
        dims_left: list[tuple[int, int]],
    ) -> list[tuple[int, ...]]:
        if not dims_left:
            return [()]
        lo, hi = dims_left[0]
        rest = cm_indices(dims_left[1:])
        out: list[tuple[int, ...]] = []
        for outer in rest:
            for i in range(lo, hi + 1):
                out.append((i,) + outer)
        return out

    return [
        IRRaw(f"{lowered.name}({', '.join(str(i) for i in idx)})")
        for idx in cm_indices(list(shape))
    ]


def _array_size_from_sym(name: Node) -> int | None:
    """1-D extent of an array Name, from the analyzer's resolved ``shape``
    attribute (``[[lo, hi]]``).  ``None`` if absent or not rank-1."""
    if name.shape is None or len(name.shape) != 1:
        return None
    lo, hi = name.shape[0]
    return hi - lo + 1


def _lower_open(node: Node) -> IRStatement:
    """``open(unit=u, file=f, status=s [,access='direct', recl=N,
    form='unformatted'])`` -> ``_units.open(u, f, s [, access, recl, form])``.

    ACCESS='DIRECT' + RECL=N enables record-based I/O via
    ``_units.read_record`` / ``read_record_raw`` (see io.hpp);
    FORM='UNFORMATTED' switches the file to raw-byte mode so the items
    pack with ``append_bytes`` / ``take_bytes``."""
    unit: IRExpr | None = None
    file: IRExpr | None = None
    status: IRExpr | None = None
    access: IRExpr | None = None
    recl: IRExpr | None = None
    form: IRExpr | None = None
    position: IRExpr | None = None
    iostat_target: IRExpr | None = None
    for cs in node.children_of_kind("ConnectSpec"):
        if cs.first_child("FileUnitNumber") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                unit = _lower_expression(e)
        elif cs.first_child("StatVariable") is not None:
            # IOSTAT=var: the OPEN's status code target.  ``_units.open``
            # returns the iostat (0 on success, nonzero on a failed open --
            # e.g. STATUS='NEW' on an existing file, STATUS='OLD' on a
            # missing one), so route it into this variable.
            iost = cs.first_child("StatVariable")
            iv = (
                iost.find_first("Variable")
                or iost.find_first("Designator")
                or iost.first_child("Expr")
            )
            if iv is not None:
                iostat_target = _lower_expression(iv)
        elif cs.first_child("StatusExpr") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                status = _lower_expression(e)
        elif cs.first_child("Recl") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                recl = _lower_expression(e)
        elif cs.first_child("CharExpr") is not None:
            ce = cs.first_child("CharExpr")
            tag = next(
                (c.kind for c in ce.children if c.kind.startswith("Kind = ")),
                None,
            )
            e = cs.find_first("Expr")
            if tag == "Kind = Access" and e is not None:
                access = _lower_expression(e)
            elif tag == "Kind = Form" and e is not None:
                form = _lower_expression(e)
            elif tag == "Kind = Position" and e is not None:
                position = _lower_expression(e)
            # BLANK=, ACTION=, etc. are not yet modeled.
        elif cs.first_child("Scalar") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                file = _lower_expression(e)
    # The runtime signature is positional:
    #   open(unit, file, status, access, recl, form, position)
    # so any specifier present forces defaults for every earlier position.
    need_form = form is not None or position is not None
    need_recl = recl is not None or need_form
    need_access = access is not None or need_recl
    need_status = status is not None or need_access
    need_file = file is not None or need_status
    args: list[IRExpr] = [unit if unit is not None else IRRaw("0")]
    if need_file:
        args.append(file if file is not None else IRRaw('""sv'))
    if need_status:
        args.append(status if status is not None else IRRaw('"unknown"sv'))
    if need_access:
        args.append(access if access is not None else IRRaw('"sequential"sv'))
    if need_recl:
        args.append(recl if recl is not None else IRRaw("0"))
    if need_form:
        args.append(form if form is not None else IRRaw('"formatted"sv'))
    if position is not None:
        args.append(position)
    if iostat_target is not None:
        # ``iostat = _units.open(...)`` -- capture the open's status.  A tail
        # arg may be needed so the (unit, file, status, ...) positions line
        # up; the runtime supplies defaults for any omitted trailing arg.
        return IRAssignment(
            target=iostat_target,
            value=IRFunctionCall(callee="_units.open", args=tuple(args)),
        )
    return IRCall(callee="_units.open", args=args)


def _lower_close(node: Node) -> IRStatement:
    """``close(u)`` -> ``_units.close(u)``.

    ``CLOSE(u, STATUS='DELETE')`` must *remove* the file, not merely
    disconnect it (the DDH file-kill path relies on this).  When a STATUS=
    specifier is present, pass it through so the runtime can honor DELETE;
    the string is evaluated at run time, so a variable status works too.
    """
    unit: IRExpr = IRRaw("0")
    status: IRExpr | None = None
    for cs in node.children_of_kind("CloseSpec"):
        if cs.first_child("FileUnitNumber") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                unit = _lower_expression(e)
        elif cs.first_child("StatusExpr") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                status = _lower_expression(e)
    if status is not None:
        return IRCall(callee="_units.close", args=[unit, status])
    return IRCall(callee="_units.close", args=[unit])


# Inquire-specifier Kind tag (``"Kind = Exist"``) -> InquireResult member
# name on the runtime side.  Any spec whose tag isn't listed here raises
# during lowering so we surface the gap loudly instead of silently
# dropping an output assignment.
_INQUIRE_FIELD: dict[str, str] = {
    "Kind = Exist":   "exist",
    "Kind = Opened":  "opened",
    "Kind = Iostat":  "iostat",
    "Kind = Number":  "number",
    "Kind = Name":    "name",
    "Kind = Named":   "opened",  # synonym for OPENED on a file selector
    "Kind = Access":  "access",
    "Kind = Form":    "form",
    "Kind = Recl":    "recl",
}


def _lower_inquire(node: Node) -> IRStatement:
    """``INQUIRE(UNIT=u, ...)`` / ``INQUIRE(FILE=f, ...)`` -- run the
    appropriate ``_units.inquire_by_*`` call and assign each requested
    spec into its target lvalue.

    AST shape: ``InquireStmt`` has one ``InquireSpec`` per clause.  The
    selector is either a ``FileUnitNumber`` (UNIT=) or a bare ``Scalar
    > DefaultChar`` (FILE=).  The output clauses are ``LogVar`` /
    ``IntVar`` / ``CharVar`` wrappers tagged with ``Kind = <Spec>``.
    """
    selector_kind: str | None = None
    selector: IRExpr | None = None
    outputs: list[tuple[str, IRExpr]] = []
    for spec in node.children_of_kind("InquireSpec"):
        fu = spec.first_child("FileUnitNumber")
        if fu is not None:
            e = fu.find_first("Expr")
            if e is not None:
                selector_kind = "unit"
                selector = _lower_expression(e)
                continue
        # Bare ``Scalar > DefaultChar`` selector: the FILE= form.
        bare_scalar = spec.first_child("Scalar")
        if bare_scalar is not None and bare_scalar.first_child(
            "DefaultChar"
        ) is not None:
            e = bare_scalar.find_first("Expr")
            if e is not None:
                selector_kind = "file"
                selector = _lower_expression(e)
                continue
        # Output specifier: a *Var wrapper with a ``Kind = ...`` child.
        var_wrap = (
            spec.first_child("LogVar")
            or spec.first_child("IntVar")
            or spec.first_child("CharVar")
        )
        if var_wrap is None:
            continue
        tag = next(
            (c.kind for c in var_wrap.children if c.kind.startswith("Kind = ")),
            None,
        )
        field = _INQUIRE_FIELD.get(tag) if tag else None
        if field is None:
            raise ConversionError(
                "INQUIRE",
                note=f"unsupported specifier {tag!r}",
                source=spec.source.text if spec.source else "",
            )
        var = var_wrap.find_first("Variable")
        if var is None:
            raise ConversionError(
                "INQUIRE",
                note=f"specifier {tag} has no target variable",
                source=spec.source.text if spec.source else "",
            )
        outputs.append((field, _lower_expression(var)))
    if selector_kind is None or selector is None:
        raise ConversionError(
            "INQUIRE",
            note="missing UNIT= or FILE= selector",
            source=node.source.text if node.source else "",
        )
    return IRInquire(
        selector_kind=selector_kind, selector=selector, outputs=outputs
    )


def _lower_file_position(node: Node, op: str) -> IRStatement:
    """``BACKSPACE(u)`` / ``REWIND(u)`` -> ``_units.<op>(u)``.

    Both accept either a bare unit expression or a positional / keyword
    spec containing the unit.  We accept the bare-unit and the
    UnitNumber positional forms (the only shapes SPICE uses); a more
    elaborate form (IOSTAT= / ERR= specs) would need extra IR fields."""
    # Direct ``FileUnitNumber`` child (positional), or nested in a
    # PositionSpec (keyword form).
    fu = node.find_first("FileUnitNumber")
    if fu is not None:
        e = fu.find_first("Expr")
        if e is not None:
            return IRFilePosition(op=op, unit=_lower_expression(e))
    # Bare unit expression (``REWIND lun``).
    e = node.first_child("Expr")
    if e is not None:
        return IRFilePosition(op=op, unit=_lower_expression(e))
    nm = node.find_first("Name")
    if nm is not None and nm.fortran:
        return IRFilePosition(
            op=op, unit=IRRaw(_safe_name(nm.fortran))
        )
    raise ConversionError(
        op.upper(),
        note="unsupported positional spec (no unit found)",
        source=node.source.text if node.source else "",
    )


def _lower_endfile(node: Node) -> IRStatement:
    """``ENDFILE(u)`` -> ``_units.endfile(u)``.  Marks the current position
    as end-of-file (truncates the backing file there)."""
    fu = node.find_first("FileUnitNumber")
    if fu is not None:
        e = fu.find_first("Expr")
        if e is not None:
            return IRCall(callee="_units.endfile", args=[_lower_expression(e)])
    e = node.first_child("Expr")
    if e is not None:
        return IRCall(callee="_units.endfile", args=[_lower_expression(e)])
    nm = node.find_first("Name")
    if nm is not None and nm.fortran:
        return IRCall(callee="_units.endfile", args=[IRRaw(_safe_name(nm.fortran))])
    raise ConversionError(
        "ENDFILE",
        note="unsupported spec (no unit found)",
        source=node.source.text if node.source else "",
    )


def _internal_file_unit(io_unit: Node | None) -> IRExpr | None:
    """If the I/O unit is a character variable (an *internal file*),
    return its lowered lvalue expression; otherwise ``None``.

    An internal-file unit appears as a ``Variable`` child of ``IoUnit``
    (``write(buf, fmt) ...``), versus a ``Star`` or ``IntLiteralConstant``
    for ``*`` / numbered external units.
    """
    if io_unit is None:
        return None
    var = io_unit.first_child("Variable")
    if var is None:
        return None
    return _lower_expression(var)


def _unit_text(io_unit: Node | None) -> str | None:
    """A simple I/O unit rendered as C++ text — an integer literal or a
    bare variable name — or ``None`` for ``*`` / an internal file / a
    unit expression too complex to render here."""
    if io_unit is None or io_unit.first_child("Star") is not None:
        return None
    if io_unit.first_child("Variable") is not None:
        return None  # internal file (character variable)
    # Render the whole unit expression so a literal (``6``), a bare variable
    # (``lun``), or an array element (``units(nest)``) all keep their full
    # form — a bare ``find_first("Name")`` would drop array subscripts.
    expr = io_unit.first_child("Expr")
    if expr is not None:
        return _render_expr_inline(_lower_expression(expr))
    nm = io_unit.find_first("Name")
    if nm is not None and nm.fortran:
        return _safe_name(nm.fortran)
    return None


def _input_stream_for_unit(io_unit: Node | None) -> str:
    u = _unit_text(io_unit)
    if u is None or u == "5":
        return "std::cin"
    # A connected file unit (or a variable unit) reads via the units table.
    return f"_units.in({u})"


def _lower_allocate(node: Node) -> IRStatement:
    """Lower ``allocate(a(n))`` / ``allocate(a(lo:hi))``.

    Only the first allocation object is handled (the common case);
    multi-object ``allocate(a(n), b(m))`` would need the dispatcher to
    return several statements, which it can't yet.
    """
    alloc = node.find_first("Allocation")
    if alloc is None:
        return _unsupported(node, kind="AllocateStmt")
    obj_node = alloc.find_first("AllocateObject")
    obj = "?"
    elem_cpp_type = ""
    if obj_node is not None:
        # The allocated object may be a derived-type component
        # (``allocate(subset%beta(...))``) — keep the full access path.
        sc = obj_node.first_child("StructureComponent")
        if sc is not None:
            path = _access_path(_lower_structure_component(sc))
            if path is not None:
                obj = path
            # The component Name carries its resolved type from flang, so
            # read the element type directly — no post-hoc resolver needed.
            comp_name = sc.first_child("Name")
            if comp_name is not None and comp_name.sym_type:
                elem_ty = _scalar_type_from_fortran(comp_name.sym_type)
                if elem_ty is not None:
                    elem_cpp_type = elem_ty.cpp
        else:
            name = obj_node.find_first("Name")
            if name is not None and name.fortran:
                obj = _safe_name(name.fortran)

    extents: list[IRExpr] = []
    lowers: list[IRExpr] = []
    has_lower = False
    for shape in alloc.find_all("AllocateShapeSpec"):
        bound_nodes = [
            c for c in shape.children if c.find_first("Expr") is not None
        ]
        bounds = [_lower_expression(b) for b in bound_nodes]
        if len(bounds) >= 2:
            lo, hi = bounds[0], bounds[1]
            has_lower = True
            lowers.append(lo)
            # extent = hi - lo + 1
            extents.append(
                IRBinaryOp(
                    op="+",
                    lhs=IRBinaryOp(op="-", lhs=hi, rhs=lo),
                    rhs=IRLiteral(cpp_text="1"),
                )
            )
        elif bounds:
            extents.append(bounds[0])
            lowers.append(IRLiteral(cpp_text="1"))
    return IRAllocate(
        obj=obj, extents=extents, lowers=lowers if has_lower else [],
        cpp_type=elem_cpp_type,
    )


def _lower_pointer_assignment(node: Node) -> IRStatement:
    """``p => target`` -> IRPointerAssign (is_array fixed up later)."""
    dataref = node.first_child("DataRef")
    name = dataref.find_first("Name") if dataref is not None else None
    ptr = _safe_name(name.fortran) if name is not None and name.fortran else "?"
    target_node = node.first_child("Expr")
    target: IRExpr | None = None
    if target_node is not None:
        lowered = _lower_expression(target_node)
        # ``p => null()`` disassociates.
        if isinstance(lowered, IRFunctionCall) and lowered.callee == "null":
            target = None
        else:
            target = lowered
    return IRPointerAssign(pointer=ptr, target=target)


def _lower_nullify(node: Node) -> IRStatement:
    """``nullify(p)`` -> a null pointer assignment (first object only)."""
    name = node.find_first("Name")
    ptr = _safe_name(name.fortran) if name is not None and name.fortran else "?"
    return IRPointerAssign(pointer=ptr, target=None)


def _resolve_pointers(sub: IRSubprogram) -> None:
    """Mark pointer-assignments to array pointers, and dereference value
    uses of scalar pointers (``p`` -> ``(*p)``)."""
    types = {loc.name: loc.type for loc in sub.locals}
    scalar_ptrs = {
        n for n, t in types.items() if t.is_pointer and not t.is_array
    }
    array_ptrs = {n for n, t in types.items() if t.is_pointer and t.is_array}

    def fix(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRPointerAssign) and stmt.pointer in array_ptrs:
            return IRPointerAssign(
                pointer=stmt.pointer, target=stmt.target, is_array=True,
                leading_comments=stmt.leading_comments,
                trailing_comments=stmt.trailing_comments,
            )
        return stmt

    sub.body = [map_statement(s, on_stmt=fix) for s in sub.body]

    if scalar_ptrs:
        def deref(e: IRExpr) -> IRExpr:
            if isinstance(e, IRName) and e.name in scalar_ptrs:
                return IRRaw(f"(*{e.name})")
            return e

        sub.body = [
            map_statement(s, on_expr=lambda e: map_expr(e, deref))
            for s in sub.body
        ]


def _lower_deallocate(node: Node) -> IRStatement:
    obj_node = node.find_first("AllocateObject")
    name = obj_node.find_first("Name") if obj_node is not None else None
    obj = _safe_name(name.fortran) if name is not None and name.fortran else "?"
    return IRDeallocate(obj=obj)


def _resolve_allocations(sub: IRSubprogram) -> None:
    """Fill in each IRAllocate's cpp_type from the declared type of its
    target (known once the subprogram's locals are lowered)."""
    types = {loc.name: loc.type.cpp for loc in sub.locals}

    def fix(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRAllocate) and stmt.obj in types:
            stmt.cpp_type = types[stmt.obj]
        return stmt

    sub.body = [map_statement(s, on_stmt=fix) for s in sub.body]


def _lower_stop(node: Node) -> IRStop:
    """Lower ``stop`` / ``stop <code>`` / ``stop "msg"`` / ``error stop``."""
    is_error = any(
        c.kind.startswith("Kind =") and "ErrorStop" in c.kind
        for c in node.children
    )
    code: IRExpr | None = None
    message: str | None = None
    stop_code = node.find_first("StopCode")
    if stop_code is not None:
        # A character stop code is a message; a numeric one is the exit
        # status.
        string_node = stop_code.find_first("string")
        if string_node is not None and string_node.fortran is not None:
            message = string_node.fortran
        else:
            expr = stop_code.find_first("Expr")
            if expr is not None:
                code = _lower_expression(expr)
    return IRStop(code=code, message=message, is_error=is_error)


def _stream_for_unit(io_unit: Node | None) -> str:
    u = _unit_text(io_unit)
    if u is None or u in ("5", "6"):
        return "std::cout"
    if u == "0":
        return "std::cerr"
    # A connected file unit (or a variable unit) writes via the units table.
    return f"_units.out({u})"


def _classify_format(node: Node) -> tuple[str, object | None]:
    """Resolve a Print/Write/Read FORMAT spec to one of:

    * ``("none", None)``      -- list-directed (``*``) or no format;
    * ``("const", str|None)`` -- a constant format string (or ``None`` when
      a label reference has no captured FORMAT statement);
    * ``("runtime", IRExpr)`` -- a format built at run time (a non-constant
      character expression), to be interpreted by the runtime.

    The positional form puts the Format as a direct child; the keyword
    form (``READ(UNIT=u, FMT=100, ...)``) buries it inside an
    IoControlSpec -- accept either."""
    fmt = node.first_child("Format")
    if fmt is None:
        for spec in node.children_of_kind("IoControlSpec"):
            nested = spec.first_child("Format")
            if nested is not None:
                fmt = nested
                break
    if fmt is None:
        return ("none", None)
    if fmt.first_child("Star") is not None:
        return ("none", None)
    # A label reference (``write(u, 100)``) -> the FORMAT statement's spec.
    label = fmt.first_child("uint64_t")
    if label is not None and label.fortran:
        try:
            return ("const", _FORMAT_LABELS.get(int(label.fortran)))
        except ValueError:
            pass
    # A character-expression format.  Prefer flang's folded constant value:
    # a concatenation of literals and PARAMETER constants
    # (``'(A,'//FMT1//')'``) is evaluated by semantics, so we read the
    # whole folded string rather than the first literal fragment.
    expr = fmt.first_child("Expr")
    if expr is not None:
        if expr.category == "constant" and expr.fortran:
            decoded = _decode_flang_char_constant(expr.fortran)
            if decoded is not None:
                return ("const", decoded)
        # A non-constant character expression (a format held in a runtime
        # variable assembled by e.g. REPMI) can't be parsed at translation
        # time -- hand the lowered expression to the runtime interpreter
        # rather than silently degrading to list-directed output.  A single
        # inline literal still falls through to the string-walk below.
        single_literal = (
            expr.first_child("LiteralConstant") is not None
            or expr.find_first("CharLiteralConstant") is not None
        )
        if not single_literal:
            return ("runtime", _lower_expression(expr))
    # Otherwise an inline character literal; pull its body.
    for s in fmt.walk():
        if s.kind == "string" and s.fortran is not None:
            return ("const", s.fortran)
    return ("none", None)


def _extract_format(node: Node) -> str | None:
    """Return the constant format string for a Print/Write/Read, or None
    for the list-directed form.

    Raises ``ConversionError`` for a runtime (non-constant) format in a
    context that can't interpret one (direct-access record I/O); the
    print/write paths call :func:`_classify_format` directly so they can
    route a runtime format through the runtime interpreter instead."""
    kind, payload = _classify_format(node)
    if kind == "runtime":
        src = node.source.text if node.source else ""
        raise ConversionError(
            "FORMAT",
            note="runtime (non-constant) format unsupported here",
            source=src,
        )
    return payload  # type: ignore[return-value]


def _decode_flang_char_constant(text: str) -> str | None:
    """Decode flang's rendering of a folded CHARACTER constant.

    Semantics renders a character constant double-quoted, with any
    embedded double-quote doubled (e.g. ``"(A,(1PE24.16))"``).  Returns
    the raw contents, or ``None`` when *text* is not such a constant."""
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('""', '"')
    return None


# Fortran intrinsic *subroutines* (invoked with CALL) that map to a
# ``ftn::`` runtime helper rather than a user-defined function.
_INTRINSIC_SUBROUTINE_MAP: dict[str, str] = {
    "cpu_time": "ftn::cpu_time",
    "system_clock": "ftn::system_clock",
    "date_and_time": "ftn::date_and_time",
    # Command-line / environment access used by the toolkit's CLI programs.
    "getarg": "ftn::getarg",
    "get_command_argument": "ftn::get_command_argument",
    "system": "ftn::system",
}


def _lower_call(node: Node) -> IRCall:
    """Lower a ``CALL <callee>(args...)`` to an :class:`IRCall`.

    Resolves the callee (handling type-bound calls' synthesized leading
    ``this`` argument), lowers and keyword-reorders the actual arguments
    against the callee's signature, and substitutes a recognized
    intrinsic-subroutine spelling when one applies."""
    call = node.first_child("Call") or node
    callee, leading = _resolve_callee(call)
    args, cats = _resolve_call_args(callee, leading, call)
    intrinsic = _INTRINSIC_SUBROUTINE_MAP.get(callee)
    if intrinsic is not None and not leading:
        return IRCall(callee=intrinsic, args=args, arg_categories=cats)
    return IRCall(callee=_safe_name(callee), args=args, arg_categories=cats)


def _lower_if_construct(node: Node) -> IRIf:
    """Lower the block-form ``IF (...) THEN ... ELSE IF ... ELSE ... END IF``
    to an :class:`IRIf` with one branch per ``(condition, body)`` pair plus
    an optional ``else_body``.

    The parse tree alternates ``Statement(IfThenStmt)`` → ``Block`` →
    ``ElseIfBlock*`` → ``ElseBlock?`` → ``Statement(EndIfStmt)``; the loop
    threads through that pattern, flushing each completed
    ``(condition, body)`` pair into the branches list."""
    branches: list[tuple[IRExpr, list[IRStatement]]] = []
    else_body: list[IRStatement] | None = None
    # Children alternate: [Statement(IfThenStmt), Block, ElseIfBlock*, ElseBlock?, Statement(EndIfStmt)]
    current_condition: IRExpr | None = None
    current_body: list[IRStatement] = []

    def flush() -> None:
        nonlocal current_condition, current_body
        if current_condition is not None:
            branches.append((current_condition, current_body))
        current_condition = None
        current_body = []

    for child in node.children:
        if child.kind == "Statement":
            if child.find_first("IfThenStmt"):
                cond_expr = child.find_first("Expr")
                current_condition = (
                    _lower_expression(cond_expr) if cond_expr else IRRaw("true")
                )
        elif child.kind == "Block":
            current_body = _lower_block(child)
            flush()
        elif child.kind == "ElseIfBlock":
            stmt = child.first_child("Statement")
            cond_expr = stmt.find_first("Expr") if stmt is not None else None
            current_condition = (
                _lower_expression(cond_expr) if cond_expr else IRRaw("true")
            )
            inner_block = child.first_child("Block")
            current_body = (
                _lower_block(inner_block) if inner_block is not None else []
            )
            flush()
        elif child.kind == "ElseBlock":
            inner_block = child.first_child("Block")
            else_body = (
                _lower_block(inner_block) if inner_block is not None else []
            )
    return IRIf(branches=branches, else_body=else_body)


def _lower_do_construct(node: Node) -> IRStatement:
    """Lower a counted ``do i = lo, hi[, step]`` loop.

    ``do while`` and ``do concurrent`` fall through to
    ``IRUnsupported`` for now.
    """
    do_stmt = None
    body_block = None
    for child in node.children:
        if child.kind == "Statement":
            if child.find_first("NonLabelDoStmt") or child.find_first(
                "LabelDoStmt"
            ):
                do_stmt = child
        elif child.kind == "Block":
            body_block = child

    if do_stmt is None:
        return _unsupported(node, kind="DoConstruct")

    loop_control = do_stmt.find_first("LoopControl")
    if loop_control is None:
        # ``do ... end do`` with no control is an infinite loop.
        body = _lower_block(body_block) if body_block else []
        return IRWhile(condition=IRLiteral(cpp_text="true"), body=body)

    # ``do concurrent (i = lo:hi[:st][, j = ...])`` — independent
    # iterations; a plain (nested) for loop is a correct translation.
    concurrent = loop_control.find_first("Concurrent")
    if concurrent is not None:
        return _lower_do_concurrent(concurrent, body_block)

    # ``do while (cond)`` — LoopControl wraps a Scalar logical expr and
    # has no LoopBounds child.
    bounds = loop_control.find_first("LoopBounds")
    if bounds is None:
        cond_expr = loop_control.find_first("Expr")
        if cond_expr is not None:
            condition = _lower_expression(cond_expr)
            body = _lower_block(body_block) if body_block else []
            return IRWhile(condition=condition, body=body)
        return _unsupported(node, kind="DoConstruct (unsupported loop control)")

    # Take the bounds from the LoopBounds' *direct* Scalar children
    # (index var, then lo / hi / step).  A recursive search would wrongly
    # pick up a nested expression — e.g. the argument of a function-call
    # bound ``do i = 1, lastnb(segid)`` would yield ``segid`` as a phantom
    # step.
    var, lo, hi, step = _lower_loop_bounds(bounds)
    body = _lower_block(body_block) if body_block else []
    return IRDo(
        var=var, lower=lo, upper=hi, step=step, body=body,
        capture_bounds=_do_bounds_modified_in_body(hi, step, body),
    )


def _do_bounds_modified_in_body(
    hi: IRExpr, step: IRExpr | None, body: list[IRStatement]
) -> bool:
    """True when a variable appearing in the loop's upper bound or step is
    assigned within the body.  Fortran fixes a counted DO's iteration count
    on entry, so such a loop must freeze its bounds -- otherwise the C++
    ``for (i = lo; i <= hi; ++i)`` re-reads the mutated ``hi`` and runs the
    wrong number of times (the SPICE f_spk21 ``DO I=J,K`` that reassigns K).
    Errs toward capturing (a bound that is only *read* is frozen to an
    identical value), so it is always safe."""
    from .emit import _render_expr

    bound_text = _render_expr(hi)
    if step is not None:
        bound_text += " " + _render_expr(step)
    names = set(re.findall(r"[A-Za-z_]\w*", bound_text))
    if not names:
        return False

    assigned: set[str] = set()

    def see(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRAssignment) and isinstance(stmt.target, IRName):
            assigned.add(stmt.target.name)
        elif isinstance(stmt, IRDo):
            assigned.add(stmt.var)
        return stmt

    for s in body:
        map_statement(s, on_stmt=see)
    return bool(names & assigned)


def _lower_do_concurrent(concurrent: Node, body_block: Node | None) -> IRStatement:
    """Lower ``do concurrent`` to a (nested) for loop.

    Each ConcurrentControl is ``name = lo:hi[:stride]``.  The index is
    construct-local, so it's declared in the for-init (declare=True);
    leftmost control is the outermost loop.
    """
    controls = list(concurrent.find_all("ConcurrentControl"))
    body: list[IRStatement] = _lower_block(body_block) if body_block else []
    return _wrap_concurrent_loops(controls, body, "empty do concurrent")


def _wrap_concurrent_loops(
    controls: list[Node], body: list[IRStatement], what: str
) -> IRStatement:
    """Wrap ``body`` in nested counted loops, one per ConcurrentControl
    (``name = lo:hi[:stride]``).  Leftmost control is the outermost loop;
    each index is construct-local (declared in the for-init)."""
    for ctrl in reversed(controls):
        name = ctrl.find_first("Name")
        var = _safe_name(name.fortran) if name is not None and name.fortran else "i"
        bounds = [
            _lower_expression(s)
            for s in ctrl.children
            if s.kind == "Scalar"
        ]
        lo = bounds[0] if bounds else IRRaw("0")
        hi = bounds[1] if len(bounds) > 1 else IRRaw("0")
        step = bounds[2] if len(bounds) > 2 else None
        body = [
            IRDo(
                var=var,
                lower=lo,
                upper=hi,
                step=step,
                body=body,
                declare=True,
            )
        ]
    return body[0] if body else _unsupported_stmt(what)


def _lower_forall(node: Node) -> IRStatement:
    """Lower a FORALL statement or construct to nested counted loops.

    FORALL evaluates each masked assignment for all index tuples; for the
    common dependence-free case this is exactly a loop nest.  The index
    set is a ConcurrentHeader (same shape as ``do concurrent``)."""
    header = node.find_first("ConcurrentHeader")
    controls = list(header.find_all("ConcurrentControl")) if header else []
    body: list[IRStatement] = []
    for asgn in node.find_all("ForallAssignmentStmt"):
        inner = asgn.first_child("AssignmentStmt")
        if inner is not None:
            body.append(_lower_assignment(inner))
            continue
        ptr = asgn.first_child("PointerAssignmentStmt")
        if ptr is not None:
            body.append(_lower_pointer_assignment(ptr))
    return _wrap_concurrent_loops(controls, body, "empty forall")


def _lower_case_construct(node: Node) -> IRStatement:
    """Lower ``select case (expr) ; case ... ; end select``."""
    selector: IRExpr = IRRaw("/* ? */")
    clauses: list[IRCaseClause] = []
    default_body: list[IRStatement] | None = None

    for child in node.children:
        if child.kind == "Statement":
            sel_stmt = child.find_first("SelectCaseStmt")
            if sel_stmt is not None:
                sel_expr = sel_stmt.find_first("Expr")
                if sel_expr is not None:
                    selector = _lower_expression(sel_expr)
        elif child.kind == "Case":
            clause, is_default = _lower_case(child)
            if is_default:
                default_body = clause.body
            else:
                clauses.append(clause)
    return IRSelectCase(
        selector=selector, clauses=clauses, default_body=default_body
    )


def _lower_case(case_node: Node) -> tuple[IRCaseClause, bool]:
    """Lower one ``Case`` (a CaseStmt + Block).  Returns (clause, is_default)."""
    values: list[IRExpr] = []
    ranges: list[tuple[IRExpr | None, IRExpr | None]] = []
    is_default = False

    case_stmt = case_node.first_child("Statement")
    if case_stmt is not None:
        selector = case_stmt.find_first("CaseSelector")
        if selector is not None:
            if selector.first_child("Default") is not None:
                is_default = True
            for vr in selector.children_of_kind("CaseValueRange"):
                _lower_case_value_range(vr, values, ranges)

    block = case_node.first_child("Block")
    body = _lower_block(block) if block is not None else []
    return IRCaseClause(values=values, ranges=ranges, body=body), is_default


def _lower_case_value_range(
    vr: Node,
    values: list[IRExpr],
    ranges: list[tuple[IRExpr | None, IRExpr | None]],
) -> None:
    """A CaseValueRange is either a single value or a (lo:hi) range."""
    range_node = vr.first_child("Range") or vr.first_child("CaseValueRange::Range")
    if range_node is not None:
        # Range form ``lo:hi`` with either bound optional.  Parse-tree shape
        # is ``tuple<optional<CaseValue>, optional<CaseValue>>`` and the empty
        # slot is dropped, so a lone present bound is positionally ambiguous.
        # The dumper's presence flags settle which side it is (no source-text
        # guessing); fall back to positional order on an older dumper.
        exprs = list(range_node.find_all("Expr"))
        lo: IRExpr | None = None
        hi: IRExpr | None = None
        if len(exprs) >= 2:
            lo = _lower_expression(exprs[0])
            hi = _lower_expression(exprs[1])
        elif len(exprs) == 1:
            single = exprs[0]
            if range_node.lower_present is False or range_node.upper_present is True:
                hi = _lower_expression(single)            # case (:hi)
            else:
                lo = _lower_expression(single)            # case (lo:)
        ranges.append((lo, hi))
        return
    expr = vr.find_first("Expr")
    if expr is not None:
        values.append(_lower_expression(expr))


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


def _lower_expression(node: Node) -> IRExpr:
    """Translate an ``Expr`` (or wrapped expression node) into IR."""
    if node is None:
        return IRRaw("/* ? */")
    # Drill through transparent wrappers (Expr, ScalarIntExpr, Constant, etc.)
    target = _drill(
        node,
        skip={
            "Expr", "Variable", "Designator", "DataRef", "ScalarIntExpr",
            "ScalarLogicalExpr", "ScalarExpr", "Scalar", "Constant",
            "Integer", "Logical", "DefaultChar",
            "ConstantExpr", "AcValue", "ActualArg",
        },
    )
    if target is None:
        return _expr_raw(node)

    match target.kind:
        case "Name":
            return _lower_name(target)
        case "LiteralConstant":
            return _lower_literal(target)
        case "IntLiteralConstant" | "SignedIntLiteralConstant":
            return _lower_int_literal(target)
        case "RealLiteralConstant" | "SignedRealLiteralConstant":
            return _lower_real_literal(target)
        case "CharLiteralConstant":
            return _lower_char_literal(target)
        case "LogicalLiteralConstant":
            return _lower_logical_literal(target)
        case "FunctionReference":
            return _lower_function_reference(target)
        case "ArrayElement":
            return _lower_array_element(target)
        case "StructureComponent":
            return _lower_structure_component(target)
        case "ArrayConstructor":
            return _lower_array_constructor(target)
        case "Substring":
            return _lower_substring(target)
    if target.kind in _BINARY_OP_MAP or target.kind in _UNARY_OP_MAP:
        return _lower_expr_operator(target)
    return _expr_raw(node)


def _lower_substring(node: Node) -> IRExpr:
    """Lower a character substring ``s(lo:hi)`` to the runtime's 1-based
    inclusive slice ``s(lo, hi)`` (``FortranString::operator()(lo, hi)``).

    Either bound may be omitted: ``s(:hi)`` defaults ``lo`` to 1, ``s(lo:)``
    defaults ``hi`` to the string's declared length."""
    dataref = node.first_child("DataRef")
    base = _lower_expression(dataref) if dataref is not None else None
    if base is None:
        return _expr_raw(node)
    rng = node.first_child("SubstringRange")
    lo: IRExpr | None = None
    hi: IRExpr | None = None
    if rng is not None:
        scalars = [c for c in rng.children if c.kind == "Scalar"]
        if len(scalars) >= 2:
            lo = _lower_expression(scalars[0])
            hi = _lower_expression(scalars[1])
        elif len(scalars) == 1:
            # One bound omitted.  The dumper's presence flags say which side
            # the lone bound belongs to (``s(:hi)`` vs ``s(lo:)``); the
            # ``SubstringRange`` source text is unavailable, so we must not
            # guess from it.  Fall back to "lower" only if flags are absent
            # (older dumper).
            if rng.lower_present is False or rng.upper_present is True:
                hi = _lower_expression(scalars[0])
            else:
                lo = _lower_expression(scalars[0])
    if lo is None:
        lo = IRLiteral(cpp_text="1")
    if hi is None:
        # Open upper bound ``s(lo:)`` -> to the end.  ``ftn::len``
        # works whether the base is a FortranString, a CharRef, or a
        # character-array element (all view as a string).
        hi = IRFunctionCall(callee="ftn::len", args=(base,))
    return IRSubstr(base=base, lo=lo, hi=hi)


def _lower_array_constructor(node: Node) -> IRExpr:
    """Lower ``[e1, e2, ...]`` to an IRArrayConstructor.

    Only the plain element-list form is handled; implied-do array
    constructors (``[(i, i=1,n)]``) fall back to a TODO.
    """
    spec = node.first_child("AcSpec")
    if spec is None:
        return _expr_raw(node)
    ac_values = spec.children_of_kind("AcValue")
    # A single bare implied-do: [(expr, i=lo,hi[,step])].
    if len(ac_values) == 1:
        impl = ac_values[0].find_first("AcImpliedDo")
        if impl is not None:
            return _lower_ac_implied_do(impl)
    elements: list[IRExpr] = []
    for ac in ac_values:
        if ac.find_first("AcImpliedDo") is not None:
            return _expr_raw(node)  # TODO: mixed / nested implied-do
        expr = ac.find_first("Expr")
        if expr is not None:
            elements.append(_lower_expression(expr))
    return IRArrayConstructor(elements=tuple(elements))


def _lower_ac_implied_do(node: Node) -> IRExpr:
    value = node.find_first("Expr")
    control = node.find_first("AcImpliedDoControl")
    lb = control.find_first("LoopBounds") if control is not None else None
    var, lo, hi, step = _lower_loop_bounds(lb)
    items = (_lower_expression(value),) if value is not None else ()
    return IRImpliedDo(var=var, lower=lo, upper=hi, step=step, items=items)


def _lower_loop_bounds(
    lb: Node | None,
) -> tuple[str, IRExpr, IRExpr, IRExpr | None]:
    """Lower a LoopBounds whose first Scalar is the index variable and
    whose remaining Scalars are lo / hi / step expressions."""
    if lb is None:
        return "i", IRRaw("1"), IRRaw("1"), None
    scalars = lb.children_of_kind("Scalar")
    var = "i"
    if scalars:
        nm = scalars[0].find_first("Name")
        if nm is not None and nm.fortran:
            var = _safe_name(nm.fortran)
    bounds = []
    for s in scalars[1:]:
        e = s.find_first("Expr")
        bounds.append(_lower_expression(e) if e is not None else IRRaw("0"))
    lo = bounds[0] if bounds else IRRaw("1")
    hi = bounds[1] if len(bounds) > 1 else IRRaw("1")
    step = bounds[2] if len(bounds) > 2 else None
    return var, lo, hi, step


def _lower_structure_component(node: Node) -> IRExpr:
    """Lower ``base%field`` to ``base.field``.

    AST shape: ``StructureComponent -> DataRef (the base) + Name (the
    component)``.  The base DataRef may itself be a StructureComponent
    or ArrayElement, so recurse through ``_lower_expression``.
    """
    base_ref = node.first_child("DataRef")
    field_name_node = None
    # The component name is the Name child that is *not* inside the
    # base DataRef.
    for child in node.children:
        if child.kind == "Name":
            field_name_node = child
    base_expr: IRExpr
    if base_ref is not None:
        base_expr = _lower_expression(base_ref)
    else:
        base_expr = IRRaw("/* ? */")
    field = (
        _safe_name(field_name_node.fortran)
        if field_name_node is not None and field_name_node.fortran
        else "?"
    )
    return IRMember(base=base_expr, field=field)


def _access_path(expr: IRExpr | None) -> str | None:
    """Render a Name / nested StructureComponent reference as a dotted
    C++ access path (``o1`` -> ``"o1"``, ``o1%cf`` -> ``"o1.cf"``), for
    use as an array-indexing base.  Returns ``None`` for bases too complex
    to spell as a simple path (e.g. ``a(i)%c``)."""
    if isinstance(expr, IRName):
        return expr.name
    if isinstance(expr, IRMember):
        base = _access_path(expr.base)
        return f"{base}.{expr.field}" if base is not None else None
    return None


def _lower_array_element(node: Node) -> IRExpr:
    """Translate ``a(i, j, k)`` to a call on the C++ Array object.

    ftn::Array overloads ``operator()`` with exactly the same
    arity / 1-based indexing as Fortran, so the translation is one
    IRFunctionCall whose callee is the array name and whose args are
    the lowered subscripts.  The indexed entity may be a derived-type
    component (``o1%beta(i, j)``), so the callee is the full access path
    of the inner DataRef rather than just its leading name.
    """
    # First child is an inner DataRef that resolves to the indexed entity
    # (a plain array name or a derived-type component).
    array_name = ""
    data_ref = node.first_child("DataRef")
    if data_ref is not None:
        path = _access_path(_lower_expression(data_ref))
        if path is not None:
            array_name = path
        else:
            name = data_ref.find_first("Name")
            if name is not None and name.fortran:
                array_name = _safe_name(name.fortran)
    raw_subs: list[IRExpr | IRTriplet] = []
    has_triplet = False
    for sub in node.children_of_kind("SectionSubscript"):
        triplet = sub.find_first("SubscriptTriplet")
        if triplet is not None:
            has_triplet = True
            raw_subs.append(_lower_subscript_triplet(triplet))
        else:
            expr = sub.find_first("Expr")
            raw_subs.append(
                _lower_expression(expr) if expr is not None else IRRaw("0")
            )
    if has_triplet:
        return IRSection(array=array_name, subscripts=tuple(raw_subs))
    # Plain element access -> call operator.
    return IRFunctionCall(
        callee=array_name,
        args=tuple(s for s in raw_subs if not isinstance(s, IRTriplet)),
    )


def _lower_subscript_triplet(triplet: Node) -> IRTriplet:
    """Lower ``lo:hi:stride`` (any part optional).

    ``SubscriptTriplet`` stores ``(lower?, upper?, stride?)`` and an omitted
    bound is dropped from the children, so position alone can't say whether a
    lone child is the lower, upper, or stride (``a(:n)`` vs ``a(n:)`` vs
    ``a(::n)``).  The dumper's per-slot presence flags settle it; the present
    children are then assigned to the present slots left to right.  Missing
    parts stay ``None`` and default to the array's bounds at expansion time."""
    bound_children = [
        c for c in triplet.children if c.find_first("Expr") is not None
    ]
    present = [
        triplet.lower_present,
        triplet.upper_present,
        triplet.stride_present,
    ]
    parts: list[IRExpr | None] = [None, None, None]
    if any(p is not None for p in present):
        # Flag-driven (current dumper): drop each present child into its slot.
        it = iter(bound_children)
        for slot, is_present in enumerate(present):
            if is_present:
                c = next(it, None)
                if c is not None:
                    parts[slot] = _lower_expression(c.find_first("Expr"))
    else:
        # Older dumper without flags: fall back to positional order.
        for i, c in enumerate(bound_children[:3]):
            parts[i] = _lower_expression(c.find_first("Expr"))
    return IRTriplet(lower=parts[0], upper=parts[1], stride=parts[2])


def _lower_name(node: Node) -> IRName:
    fortran = node.fortran or (node.source.text if node.source else "?")
    return IRName(name=_safe_name(fortran), fortran=fortran)


def _lower_literal(node: Node) -> IRExpr:
    inner = next(iter(node.children), None)
    if inner is None:
        return _expr_raw(node)
    return _lower_expression(inner)


# Match ``42`` / ``42_4`` / ``42_8`` / ``-3_8`` etc.
_INT_LITERAL_RE = re.compile(r"^([+-]?\d+)(?:_(\d+))?$")


def _lower_int_literal(node: Node) -> IRLiteral:
    """Integer literal with Fortran kind preserved in the C++ form."""
    raw = (node.fortran or "0").strip()
    m = _INT_LITERAL_RE.match(raw)
    if not m:
        return IRLiteral(cpp_text=raw)
    value, kind = m.group(1), m.group(2)
    # Fortran integer literals are decimal; strip any leading zeros so C++
    # doesn't read ``08`` / ``09`` as a (invalid) octal constant.
    value = str(int(value))
    k = int(kind) if kind else 4
    if k <= 4:
        return IRLiteral(cpp_text=value, cpp_type="int32_t")
    return IRLiteral(cpp_text=f"{value}LL", cpp_type="int64_t")


# Match ``1.0`` / ``1.0_4`` / ``1.0d0`` / ``-1.5e-3_8`` etc.
_REAL_LITERAL_RE = re.compile(
    r"^([+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eEdD][+-]?\d+)?)"
    r"(?:_(\d+))?$"
)


def _lower_real_literal(node: Node) -> IRLiteral:
    """Real literal, mapped to ``float`` or ``double``."""
    # The literal value lives on the ``Real`` child's ``fortran`` field
    # (set by flang's dump-parse-tree.h special case).
    raw_node = node.first_child("Real") or node.first_child("RealLiteralConstant::Real")
    raw = (raw_node.fortran if raw_node and raw_node.fortran
           else (node.fortran or "0.0")).strip()
    m = _REAL_LITERAL_RE.match(raw)
    if not m:
        return IRLiteral(cpp_text=raw)
    body, kind = m.group(1), m.group(2)
    # ``d0`` exponent means double precision regardless of explicit kind.
    is_double = bool(re.search(r"[dD]", body)) or (kind == "8")
    # Normalize d/D in exponent to e.
    cpp_body = re.sub(r"[dD]", "e", body)
    if is_double:
        return IRLiteral(cpp_text=cpp_body, cpp_type="double")
    return IRLiteral(cpp_text=cpp_body + "f", cpp_type="float")


def _lower_char_literal(node: Node) -> IRLiteral:
    """Character literal — always rendered as a ``string_view`` (R5)."""
    # The unescaped body lives on the child ``string`` node's ``fortran``.
    body_node = node.first_child("string") or node.first_child("std::string")
    body = body_node.fortran if body_node and body_node.fortran is not None else ""
    if not body and node.source is not None:
        # Fallback: rip the body out of the source text directly.
        body = _fortran_char_literal_body(node.source.text)
    cpp = '"' + body.replace("\\", "\\\\").replace('"', '\\"') + '"sv'
    return IRLiteral(cpp_text=cpp)


def _lower_logical_literal(node: Node) -> IRLiteral:
    # Carry ``cpp_type="bool"`` so a homogeneous logical DATA table (e.g.
    # ``DATA flags / 128*.FALSE. /``) qualifies for static-constexpr table
    # extraction instead of a giant ``array_of(false, false, ...)`` that
    # overflows std::common_type's argument fold.
    body_node = node.first_child("bool")
    if body_node and body_node.fortran is not None:
        return IRLiteral(
            cpp_text="true" if body_node.fortran == "true" else "false",
            cpp_type="bool",
        )
    raw = (node.source.text if node.source else "").lower().strip(". ")
    return IRLiteral(
        cpp_text="true" if "t" in raw[:1] else "false", cpp_type="bool"
    )


# Fortran intrinsics that map directly to a name in <cmath> / std::.
# Anything not in this table is emitted as a plain call; the user's
# own functions therefore "just work" as long as they have a C++
# definition (typically a translated sibling subprogram).
_INTRINSIC_MAP: dict[str, str] = {
    # Elemental math -> ftn:: overloads that map over arrays as well
    # as scalars (intrinsics.hpp); scalar calls delegate to std::.
    "sqrt": "ftn::sqrt", "abs": "ftn::abs", "exp": "ftn::exp",
    "log": "ftn::log", "log10": "ftn::log10",
    "sin": "ftn::sin", "cos": "ftn::cos", "tan": "ftn::tan",
    "asin": "ftn::asin", "acos": "ftn::acos", "atan": "ftn::atan",
    "atan2": "std::atan2", "sinh": "ftn::sinh", "cosh": "ftn::cosh",
    "tanh": "ftn::tanh", "floor": "std::floor", "ceiling": "std::ceil",
    "min": "ftn::min", "max": "ftn::max",
    # Command-line / environment query intrinsics (functions).
    "iargc": "ftn::iargc", "nargs": "ftn::nargs",
    "getenvqq": "ftn::getenvqq", "systemqq": "ftn::systemqq",
    "getlasterrorqq": "ftn::getlasterrorqq",
    # Bit-manipulation intrinsics.
    "iand": "ftn::iand", "ior": "ftn::ior", "ieor": "ftn::ieor",
    "ishft": "ftn::ishft", "btest": "ftn::btest",
    "ibset": "ftn::ibset", "ibclr": "ftn::ibclr",
    # Numeric inquiry intrinsics.
    "huge": "ftn::huge", "tiny": "ftn::tiny",
    "epsilon": "ftn::epsilon", "kind": "ftn::kind",
    "bit_size": "ftn::bit_size", "precision": "ftn::precision",
    "radix": "ftn::radix", "digits": "ftn::digits",
    # Character <-> integer intrinsics.
    "achar": "ftn::achar", "char": "ftn::achar",
    "iachar": "ftn::ichar", "ichar": "ftn::ichar",
    "mod": "ftn::mod",       # generic: integer % or std::fmod
    "amod": "ftn::mod", "dmod": "ftn::mod",  # real/double specifics
    "modulo": "ftn::modulo",  # remainder with sign of divisor
    "merge": "ftn::merge",
    "sign": "std::copysign", "dsign": "std::copysign",
    # FORTRAN 77 type-specific intrinsic spellings -> the generic forms.
    "alog": "ftn::log", "dlog": "ftn::log",
    "alog10": "ftn::log10", "dlog10": "ftn::log10",
    "dsqrt": "ftn::sqrt", "dexp": "ftn::exp",
    "dabs": "ftn::abs", "iabs": "ftn::abs",
    "dsin": "ftn::sin", "dcos": "ftn::cos", "dtan": "ftn::tan",
    "dasin": "ftn::asin", "dacos": "ftn::acos", "datan": "ftn::atan",
    "datan2": "std::atan2", "dsinh": "ftn::sinh",
    "dcosh": "ftn::cosh", "dtanh": "ftn::tanh",
    "amax1": "ftn::max", "dmax1": "ftn::max", "max0": "ftn::max",
    "amax0": "ftn::max",
    "amin1": "ftn::min", "dmin1": "ftn::min", "min0": "ftn::min",
    "amin0": "ftn::min",
    "dnint": "ftn::anint", "idnint": "ftn::nint",
    # Array intrinsics -> ftn:: runtime helpers (intrinsics.hpp).
    "size": "ftn::size", "lbound": "ftn::lbound",
    "ubound": "ftn::ubound", "sum": "ftn::sum",
    "product": "ftn::product", "maxval": "ftn::maxval",
    "minval": "ftn::minval", "count": "ftn::count",
    "any": "ftn::any", "all": "ftn::all",
    "dot_product": "ftn::dot_product",
    "matmul": "ftn::matmul", "transpose": "ftn::transpose",
    "maxloc": "ftn::maxloc", "minloc": "ftn::minloc",
    "pack": "ftn::pack", "cshift": "ftn::cshift",
    "eoshift": "ftn::eoshift", "spread": "ftn::spread",
    # Character intrinsics.
    "trim": "ftn::trim", "len": "ftn::len",
    "len_trim": "ftn::len_trim", "index": "ftn::index",
    "adjustl": "ftn::adjustl", "adjustr": "ftn::adjustr",
    "repeat": "ftn::repeat", "scan": "ftn::scan",
    "verify": "ftn::verify",
    # Rounding / truncating conversions (plain int/real/dble are casts,
    # handled separately in _lower_conversion_intrinsic).
    "nint": "ftn::nint", "aint": "ftn::aint",
    "anint": "ftn::anint", "dint": "ftn::aint",
    # Lexical (collating-sequence) string comparisons.
    "llt": "ftn::llt", "lle": "ftn::lle",
    "lgt": "ftn::lgt", "lge": "ftn::lge",
}


# Kind-dependent numeric conversion intrinsics -> C++ casts.  The
# target C++ type depends on the (optional) kind argument.
_INT_KIND_CPP = {
    None: "int32_t", 1: "int8_t", 2: "int16_t",
    4: "int32_t", 8: "int64_t",
}
_REAL_KIND_CPP = {None: "float", 4: "float", 8: "double"}


def _lower_function_reference(node: Node) -> IRExpr:
    """Lower a function call expression to an :class:`IRFunctionCall` or
    one of the special-shape expressions.

    Handles, in this order:

    * ``PRESENT(x)`` → ``x.has_value()`` (optional scalar test);
    * ``ASSOCIATED(p)`` → ``ftn::associated(p)``;
    * conversion intrinsics (``INT``, ``REAL``, ``DBLE``, ``FLOAT``,
      ``IFIX``, ``IDINT``, ``DFLOAT``, ``SNGL``) → ``IRCast``;
    * ``RESHAPE(src, [d1, d2, ...])`` with a literal shape → a fixed-rank
      ``ftn::reshape(src, d1, d2, ...)`` so the rank deduces at
      compile time;
    * everything else — a user function or a plain intrinsic — emits an
      ``IRFunctionCall`` whose ``arg_categories`` carry each actual's
      resolved AST category (variable / constant / expression) for the
      downstream ``_materialize_value_args`` pass."""
    call = node.first_child("Call") or node
    callee, leading = _resolve_callee(call)
    args, cats = _resolve_call_args(callee, leading, call)

    # present(x) -> x.has_value() (x is a std::optional param).  Use the
    # raw optional name; the deref pass won't touch this IRRaw.
    if callee == "present" and len(args) == 1 and isinstance(args[0], IRName):
        return IRRaw(f"({args[0].name}.has_value())")

    # associated(p) -> ftn::associated(p) using the raw pointer name
    # (not the deref'd value), overloaded for T* and ArrayRef.
    if callee == "associated" and len(args) == 1 and isinstance(args[0], IRName):
        return IRRaw(f"ftn::associated({args[0].name})")

    # Conversion intrinsics become static_casts whose target type
    # depends on the kind argument.
    conv = _lower_conversion_intrinsic(callee, args)
    if conv is not None:
        return conv

    # reshape(source, [d1, d2, ...]) -> ftn::reshape(source, d1, d2, ...)
    # so the result rank is deduced from the (literal) shape's length.
    if callee == "reshape" and len(args) >= 2 and isinstance(
        args[1], IRArrayConstructor
    ):
        flat = (args[0], *args[1].elements)
        return IRFunctionCall(callee="ftn::reshape", args=flat)

    # Not an intrinsic -> a user function; safe-name it to match the
    # (safe-named) subprogram definition.
    cpp_callee = _INTRINSIC_MAP.get(callee, _safe_name(callee))
    return IRFunctionCall(callee=cpp_callee, args=tuple(args), arg_categories=cats)


def _lower_conversion_intrinsic(
    callee: str, args: list[IRExpr]
) -> IRExpr | None:
    """Lower INT / REAL / DBLE / FLOAT to a static_cast, honoring an
    optional kind argument; return None for non-conversion callees."""
    if not args:
        return None
    operand = args[0]
    kind = _literal_int_value(args[1]) if len(args) > 1 else None
    if callee in ("int", "ifix", "idint"):  # ifix/idint: F77 real/double -> int
        return IRCast(cpp_type=_INT_KIND_CPP.get(kind, "int32_t"),
                      operand=operand)
    if callee in ("real", "float"):
        return IRCast(cpp_type=_REAL_KIND_CPP.get(kind, "float"),
                      operand=operand)
    if callee in ("dble", "dfloat"):
        return IRCast(cpp_type="double", operand=operand)
    if callee == "sngl":  # double -> single precision
        return IRCast(cpp_type="float", operand=operand)
    return None


def _literal_int_value(expr: IRExpr) -> int | None:
    if isinstance(expr, IRLiteral):
        try:
            return int(expr.cpp_text.rstrip("Ll"))
        except ValueError:
            return None
    return None


def _callee_name(call: Node) -> str:
    """Pull the procedure name out of a Call's ProcedureDesignator
    (plain calls only; type-bound calls go through _resolve_callee)."""
    name, _ = _resolve_callee(call)
    return name


def _resolve_callee(call: Node) -> tuple[str, list[IRExpr]]:
    """Return (callee_name, leading_args).

    For a type-bound call ``obj%method(...)`` the passed object becomes
    the first argument (the PASS convention): returns
    ``("method", [obj])``.  For a plain call returns ``(name, [])``.
    """
    desig = call.first_child("ProcedureDesignator")
    if desig is None:
        return "", []
    pcr = desig.find_first("ProcComponentRef")
    if pcr is not None:
        sc = pcr.find_first("StructureComponent")
        if sc is not None:
            obj_ref = sc.first_child("DataRef")
            method = sc.first_child("Name")  # direct child: the binding
            obj_expr = (
                _lower_expression(obj_ref)
                if obj_ref is not None
                else IRRaw("/* ? */")
            )
            mname = (
                method.fortran.lower()
                if method is not None and method.fortran
                else "?"
            )
            return mname, [obj_expr]
    name = desig.first_child("Name")
    if name is not None and name.fortran:
        # Raw lower-case: intrinsic matching happens on this name; the
        # caller safe-names it only if it resolves to a user function.
        return name.fortran.lower(), []
    return "", []


def _lower_actual_args(call: Node) -> list[IRExpr]:
    """Lower the *direct* actual arguments of a Call (positional order).

    Uses direct children (not a recursive search) so a nested call's
    own arguments aren't mistaken for this call's.
    """
    return [expr for _, expr, _cat in _lower_actual_arg_pairs(call)]


def _lower_actual_arg_pairs(
    call: Node,
) -> list[tuple[str | None, IRExpr, str | None]]:
    """Like :func:`_lower_actual_args` but pairs each argument with its
    keyword name (or ``None`` for a positional argument) and the AST
    ``category`` on the analyzed ``Expr`` — ``"variable"`` / ``"constant"``
    / ``"expression"`` — for downstream passes that decide rvalue handling
    (notably ``_materialize_value_args``)."""
    pairs: list[tuple[str | None, IRExpr, str | None]] = []
    for arg in call.children_of_kind("ActualArgSpec"):
        kw: str | None = None
        kw_node = arg.first_child("Keyword")
        if kw_node is not None:
            kw_name = kw_node.find_first("Name")
            if kw_name is not None and kw_name.fortran:
                kw = _safe_name(kw_name.fortran)
        expr = arg.find_first("Expr")
        if expr is None:
            # The arg may be an ActualArg wrapper around the expression.
            actual = arg.first_child("ActualArg")
            if actual is not None:
                expr = actual.find_first("Expr")
        if expr is not None:
            pairs.append((kw, _lower_expression(expr), expr.category))
    return pairs


def _resolve_call_args(
    callee: str, leading: list[IRExpr], call: Node
) -> tuple[list[IRExpr], tuple[str, ...]]:
    """Build the final positional argument list for a call (with each
    argument's resolved AST category), applying keyword-argument reordering
    against the callee's known signature.

    ``leading`` holds any synthetic leading arguments (the passed object
    of a type-bound call); the callee's first dummy corresponds to it and
    is dropped before matching the explicit keyword arguments.  Leading
    args don't come from source ``Expr``\\ s and so have no category.
    """
    triples = _lower_actual_arg_pairs(call)
    dummies = _SIGNATURES.get(callee, [])
    if leading and dummies:
        dummies = dummies[1:]
    reordered, cats = _reorder_keyword_args(triples, dummies)
    return leading + reordered, ("",) * len(leading) + cats


def _reorder_keyword_args(
    triples: list[tuple[str | None, IRExpr, str | None]],
    dummies: list[tuple[str, bool]],
) -> tuple[list[IRExpr], tuple[str, ...]]:
    """Reorder ``(keyword, expr, category)`` triples into positional order
    using the callee's ordered ``(dummy_name, is_optional)`` list.

    Positional args fill slots left to right; keyword args drop into their
    named slot.  A gap left by an omitted OPTIONAL argument is filled with
    ``std::nullopt`` so the remaining positional arguments stay aligned; a
    *trailing* run of omitted optionals is dropped entirely (the C++
    default argument supplies ``std::nullopt``).  With no keywords (or an
    unknown callee) the original positional order is preserved.  Returns
    the ordered ``(args, categories)`` — categories mirror ``args``
    (``""`` for a synthesized ``std::nullopt`` filler)."""
    if not any(kw is not None for kw, _, _ in triples) or not dummies:
        return (
            [expr for _, expr, _ in triples],
            tuple(cat or "" for _, _, cat in triples),
        )
    names = [n for n, _ in dummies]
    slots: list[tuple[IRExpr, str] | None] = [None] * len(dummies)
    extra: list[tuple[IRExpr, str]] = []
    pos = 0
    for kw, expr, cat in triples:
        cat_s = cat or ""
        if kw is None:
            if pos < len(slots):
                slots[pos] = (expr, cat_s)
            else:
                extra.append((expr, cat_s))
            pos += 1
        elif kw in names:
            slots[names.index(kw)] = (expr, cat_s)
        else:
            extra.append((expr, cat_s))
    # Drop the trailing run of unfilled optional slots (C++ defaults them).
    last = len(slots)
    while last > 0 and slots[last - 1] is None and dummies[last - 1][1]:
        last -= 1
    result_args: list[IRExpr] = []
    result_cats: list[str] = []
    for i in range(last):
        s = slots[i]
        if s is not None:
            result_args.append(s[0])
            result_cats.append(s[1])
        elif dummies[i][1]:
            result_args.append(IRRaw("std::nullopt"))
            result_cats.append("")
        # An unfilled non-optional slot can't happen for valid Fortran;
        # skip it rather than emit a bogus argument.
    for e, c in extra:
        result_args.append(e)
        result_cats.append(c)
    return result_args, tuple(result_cats)


# Map Expr operator subclasses to the C++ operator we want to emit.
# The dump-parse-tree NODE(Expr, Add) macro yields just ``"Add"`` (not
# ``"Expr::Add"``) — see flang/include/flang/Parser/dump-parse-tree.h.
_BINARY_OP_MAP: dict[str, str] = {
    "Add": "+", "Subtract": "-",
    "Multiply": "*", "Divide": "/",
    "LT": "<", "LE": "<=",
    "GT": ">", "GE": ">=",
    "EQ": "==", "NE": "!=",
    "AND": "&&", "OR": "||",
    "EQV": "==", "NEQV": "!=",
    "Power": "**",         # special-cased below -> std::pow
    "Concat": "//",        # special-cased below -> ftn::concat
    # NB: user-defined operators (``DefinedBinary``/``DefinedUnary``) are
    # deliberately absent: rather than emit a bogus ``a ? b`` they fall
    # through to ``_expr_raw`` and raise a clean ConversionError.
}

_UNARY_OP_MAP: dict[str, str] = {
    "Negate": "-",
    "UnaryPlus": "+",
    "NOT": "!",
    "Parentheses": "()",   # special-cased in the emitter
}


def _lower_expr_operator(node: Node) -> IRExpr:
    kind = node.kind
    if kind in _BINARY_OP_MAP:
        operands = [c for c in node.children if c.kind == "Expr"]
        if len(operands) >= 2:
            lhs = _lower_expression(operands[0])
            rhs = _lower_expression(operands[1])
            if kind == "Power":
                return IRFunctionCall(callee="std::pow", args=(lhs, rhs))
            if kind == "Concat":
                return IRFunctionCall(callee="ftn::concat", args=(lhs, rhs))
            return IRBinaryOp(op=_BINARY_OP_MAP[kind], lhs=lhs, rhs=rhs)
    if kind in _UNARY_OP_MAP:
        operand = next((c for c in node.children if c.kind == "Expr"), None)
        if operand is not None:
            inner = _lower_expression(operand)
            if kind == "Parentheses":
                return IRUnaryOp(op="()", operand=inner)
            return IRUnaryOp(op=_UNARY_OP_MAP[kind], operand=inner)
    return _expr_raw(node)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drill(node: Node, *, skip: Iterable[str]) -> Node | None:
    """Descend through single-child nodes whose kind is in ``skip``."""
    skip_set = set(skip)
    cur = node
    while cur.kind in skip_set:
        if len(cur.children) == 1:
            cur = cur.children[0]
        else:
            # Pick the first non-trivial child.
            picked = next(
                (c for c in cur.children if c.kind not in skip_set), None
            )
            if picked is None:
                return cur
            cur = picked
    return cur


def _expr_raw(node: Node) -> NoReturn:
    """An expression node the converter doesn't model.  Fail loudly rather
    than emit a ``/* TODO */`` placeholder that compiles to garbage."""
    src = node.source.text if node.source else node.kind
    raise ConversionError(node.kind, note="unsupported expression", source=src)


def _unsupported(
    node: Node, *, kind: str, leading: list[Comment] | None = None
) -> NoReturn:
    """A statement / construct the converter doesn't model — fail loudly."""
    src = node.source.text if node.source else ""
    raise ConversionError(kind, source=src)


def _fortran_char_literal_body(raw: str) -> str:
    """Strip surrounding Fortran quotes and undo doubled-quote escapes."""
    raw = raw.strip()
    if len(raw) < 2 or raw[0] not in ("'", '"') or raw[-1] != raw[0]:
        return raw
    q = raw[0]
    return raw[1:-1].replace(q + q, q)
