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
from typing import Iterable, Literal

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
    IREntry,
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
    return tu


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
    for sub in tu.subprograms:
        param_names = {p.name for p in sub.parameters}
        called = _called_names(sub.body)
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
        # Any scalar (non-array, non-pointer) param is a const candidate —
        # including one a prior (per-file) run already marked ``in``, so a
        # later whole-program run can correct it back to ``inout`` once a
        # cross-file callee that writes it becomes visible.
        scalar_inout[sub.name] = {
            p.name
            for p in sub.parameters
            if p.intent in ("in", "inout")
            and not p.type.is_array
            and not p.type.is_pointer
            and not p.type.is_procedure
        }
        w: set[str] = set()
        e: list[tuple[str, str, int]] = []

        def handle_call(callee: str, args, *, _e=e, _pn=pnames) -> None:
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


def _is_array_element(expr: IRExpr, subprograms: dict) -> bool:
    """True if ``expr`` indexes an array (``a(i)``, ``v%c(i)``) rather than
    calls a function.  An array access lowers to a call-shaped node whose
    callee is an access path, not a known subprogram or a ``fortran::`` /
    ``std::`` intrinsic."""
    return (
        isinstance(expr, IRFunctionCall)
        and "::" not in expr.callee
        and expr.callee not in subprograms
        and len(expr.args) >= 1
    )


def _reshape_sequence_associated_args(tu: IRTranslationUnit) -> None:
    """Fortran sequence association: a contiguous rank-1 actual passed to
    a higher-rank, explicit-shape dummy.  Wrap such an actual in
    ``fortran::seq_assoc<R>(..., {lowers}, {extents})`` using the dummy's
    declared shape, so the call type-checks.  Runs before state plumbing
    so call arguments still line up with the callee's Fortran dummies."""
    params_by_name = {s.name: s.parameters for s in tu.subprograms}

    def reshape(callee: str, args: list[IRExpr]) -> list[IRExpr]:
        params = params_by_name.get(callee)
        if not params:
            return args
        out = list(args)
        for i, p in enumerate(params):
            if i >= len(out):
                break
            if not p.type.is_array:
                continue
            actual = out[i]
            if (
                p.type.array_rank >= 2
                and p.type.array_extent_exprs
                and _expr_rank(actual) == 1
            ):
                rank = p.type.array_rank
                lowers = p.type.array_lower_bound_exprs or ["1"] * rank
                extents = list(p.type.array_extent_exprs)
                out[i] = IRFunctionCall(
                    callee=f"fortran::seq_assoc<{rank}>",
                    args=(
                        actual,
                        IRRaw("{" + ", ".join(lowers) + "}"),
                        IRRaw("{" + ", ".join(extents) + "}"),
                    ),
                )
            elif p.type.array_rank == 1 and _is_array_element(actual, params_by_name):
                # ``call s(a(i,j))`` with an array dummy: the dummy views
                # the storage from that element onward (sequence assoc).
                out[i] = IRFunctionCall(
                    callee="fortran::elem_tail",
                    args=(
                        IRName(name=actual.callee, fortran=actual.callee),
                        *actual.args,
                    ),
                )
        return out

    def fix_stmt(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRCall):
            return IRCall(
                callee=stmt.callee,
                args=reshape(stmt.callee, list(stmt.args)),
                leading_comments=stmt.leading_comments,
                trailing_comments=stmt.trailing_comments,
            )
        return stmt

    def fix_expr(expr: IRExpr) -> IRExpr:
        if isinstance(expr, IRFunctionCall):
            return IRFunctionCall(
                callee=expr.callee, args=tuple(reshape(expr.callee, list(expr.args)))
            )
        return expr

    for sub in tu.subprograms:
        sub.body = [
            map_statement(
                s, on_stmt=fix_stmt, on_expr=lambda e: map_expr(e, fix_expr)
            )
            for s in sub.body
        ]


def _set_proc_signature(p: IRParameter, arg_types: tuple[str, ...]) -> None:
    """Rewrite a dummy-procedure parameter's ``std::function<R(...)>`` type
    to take exactly ``arg_types``, keeping its result type R."""
    cpp = p.type.cpp
    ret = cpp[len("std::function<"): cpp.index("(")]
    p.type = dataclasses.replace(
        p.type,
        cpp=f"std::function<{ret}({', '.join(arg_types)})>",
        proc_arity=len(arg_types),
    )


def _infer_procedure_arities(tu: IRTranslationUnit) -> None:
    """Whole-program inference of dummy-procedure *signatures*.

    Under FORTRAN 77's implicit interface a routine has no signature for a
    procedure passed to it, so the per-routine pass models every dummy
    procedure as ``std::function<R(float, ...)>`` with an argument count
    guessed from local calls (defaulting to 1).  That is wrong both in
    arity — a forwarded procedure such as ``GFBAIL`` (0 args) or ``GFREFN``
    (5 args) is never called locally — and in argument *types*: ``GFREFN``
    takes ``double`` / ``logical`` arguments, not ``float``, so a
    ``float``-typed wrapper cannot bind its ``double&`` outputs.

    The actual procedures passed in are known subprograms, so propagate
    their real parameter types across the call graph until stable:

      * passing a known subprogram as an actual gives the receiving
        dummy-procedure parameter that subprogram's exact parameter types;
      * *forwarding* one routine's dummy procedure into another's slot ties
        the two signatures together (a ``std::function`` actual binds only
        to an identical ``std::function`` type).

    A dummy procedure never reached by a concrete actual keeps its original
    ``float``-based guess.
    """
    by_name = {s.name: s for s in tu.subprograms}

    def proc_param(sub: IRSubprogram, name: str) -> IRParameter | None:
        for p in sub.parameters:
            if p.name == name and p.type.is_procedure:
                return p
        return None

    def sig_of(actual: IRSubprogram) -> tuple[str, ...]:
        return tuple(pp.cpp_param_type() for pp in actual.parameters)

    # (sub_name, param_name) -> inferred argument-type tuple.
    models: dict[tuple[str, str], tuple[str, ...]] = {}

    def bump(key: tuple[str, str], cand: tuple[str, ...]) -> bool:
        """Grow ``key``'s inferred signature towards ``cand``.  Monotonic:
        a slot only ever gains arguments (the widest actual seen wins, ties
        keep the incumbent), so the fixpoint always terminates even when
        two incompatible actuals reach the same dummy-procedure slot."""
        cur = models.get(key)
        if cur is None or len(cand) > len(cur):
            models[key] = cand
            return True
        return False

    changed = True
    while changed:
        changed = False
        for caller in tu.subprograms:

            def visit(callee_name: str, args) -> None:
                nonlocal changed
                callee = by_name.get(callee_name)
                if callee is None:
                    return
                for i, a in enumerate(args):
                    if i >= len(callee.parameters):
                        break
                    q = callee.parameters[i]
                    if not q.type.is_procedure or not isinstance(a, IRName):
                        continue
                    qkey = (callee.name, q.name)
                    actual = by_name.get(a.name)
                    if actual is not None:
                        cand = sig_of(actual)
                    else:
                        fwd = proc_param(caller, a.name)
                        if fwd is None:
                            continue
                        cand = models.get((caller.name, fwd.name))
                        # Forwarding ties the two signatures together.
                        if qkey in models and bump((caller.name, fwd.name), models[qkey]):
                            changed = True
                    if cand is not None and bump(qkey, cand):
                        changed = True

            def on_stmt(s: IRStatement) -> IRStatement:
                if isinstance(s, IRCall):
                    visit(s.callee, s.args)
                return s

            def on_expr(e: IRExpr) -> IRExpr:
                if isinstance(e, IRFunctionCall):
                    visit(e.callee, e.args)
                return e

            for s in caller.body:
                map_statement(
                    s, on_stmt=on_stmt, on_expr=lambda e: map_expr(e, on_expr)
                )

    for sub in tu.subprograms:
        for p in sub.parameters:
            if p.type.is_procedure and (sub.name, p.name) in models:
                _set_proc_signature(p, models[(sub.name, p.name)])


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
    """Wrap logical items of list-directed ``print`` in ``fortran::
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
                    IRFunctionCall(callee="fortran::logical_text", args=(it,))
                    if _is_logical_expr(it, logical_names)
                    else it
                    for it in stmt.items
                ]
            return stmt

        sub.body = [map_statement(s, on_stmt=fix) for s in sub.body]


def _materialize_value_args(tu: IRTranslationUnit) -> None:
    """Pass a constant/expression actual through ``fortran::byref`` when
    the dummy is a modifiable scalar reference.

    Fortran lets any expression be an actual argument; for an INOUT/OUT
    dummy the compiler binds a temporary (copy-in, write-back discarded).
    C++ won't bind a non-const ``T&`` to an rvalue, so ``call s(x, 0.0)``
    fails to compile.  ``fortran::byref`` materializes the value into an
    lvalue whose lifetime spans the call, restoring Fortran's behavior
    (it is a no-op for an lvalue actual, which still binds directly)."""
    by_name = {s.name: s for s in tu.subprograms}
    if not by_name:
        return
    # Unambiguous rvalues — a literal, an arithmetic/relational result, or
    # a cast.  Designators (names, array elements, sections, components)
    # are left alone: they already bind to a reference.
    rvalue_nodes = (IRLiteral, IRBinaryOp, IRUnaryOp, IRCast)

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

    def wrap(callee: str, args) -> list:
        out = []
        for i, a in enumerate(args):
            if isinstance(a, rvalue_nodes) and wants_ref(callee, i):
                out.append(IRFunctionCall(callee="fortran::byref", args=(a,)))
            else:
                out.append(a)
        return out

    def on_expr(e: IRExpr) -> IRExpr:
        if isinstance(e, IRFunctionCall) and e.callee in by_name:
            return IRFunctionCall(callee=e.callee, args=tuple(wrap(e.callee, e.args)))
        return e

    def on_stmt(s: IRStatement) -> IRStatement:
        if isinstance(s, IRCall) and s.callee in by_name:
            s.args = wrap(s.callee, s.args)
        return s

    for sub in tu.subprograms:
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


def _lower_main_program(node: Node) -> IRSubprogram:
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
        leading_comments=list(node.leading_comments),
        source=node.source,
    )
    _lower_specification_and_execution(node, sub)
    return sub


def _lower_function(node: Node) -> IRSubprogram:
    name = _extract_subprogram_name(node, "FunctionStmt") or "anon_function"
    sub = IRSubprogram(
        name=_safe_name(name),
        display_name=name,
        kind="function",
        leading_comments=list(node.leading_comments),
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


def _lower_subroutine(node: Node) -> IRSubprogram:
    name = _extract_subprogram_name(node, "SubroutineStmt") or "anon_subroutine"
    sub = IRSubprogram(
        name=_safe_name(name),
        display_name=name,
        kind="subroutine",
        leading_comments=list(node.leading_comments),
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
    """If the function has a leading type prefix (``real function f``),
    lower it here so we can short-circuit the "function-named local"
    search later."""
    for stmt in subprog.children:
        if stmt.kind != "Statement":
            continue
        func_stmt = stmt.find_first("FunctionStmt")
        if func_stmt is None:
            continue
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
        remaining = [
            loc
            for loc in remaining
            if loc.name not in externals or isinstance(loc.initializer, IRLambda)
        ]
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


def _count_call_arity(body: list[IRStatement], name: str) -> int:
    """Largest argument count among calls to ``name`` in ``body`` (both
    function-call expressions and subroutine-call statements)."""
    best = 1

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
    return best


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
        return IRType(cpp="std::int32_t", fortran="integer", is_integer=True)
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
    # DATA initializations run after implicit typing so array-vs-scalar is
    # known, and before the executable body.  Collected unit-wide because
    # F77 allows DATA among executable statements, not just declarations.
    data_inits = _lower_data_statements(node, sub.locals)
    sub.body = data_inits + sub.body
    _resolve_allocations(sub)
    _resolve_pointers(sub)
    _expand_array_assignments(sub)
    # ENTRY statements split this unit into several alternate entry points
    # that share its storage.  Carve each one out (statements from the
    # entry onward) as its own subprogram before goto-structuring, which
    # rewrites the body irreversibly.
    _split_entry_points(sub, node, data_inits)
    # Eliminate goto in favor of structured control flow.
    sub.body, used_dispatch = structure_gotos(sub.body)
    # ``structure_gotos`` only reports a *top-level* dispatch; a dispatch
    # created inside a nested loop body still references ``_pc``, so scan
    # the structured body to be sure the state local is declared.
    if used_dispatch or _references_name(sub.body, "_pc"):
        sub.locals.append(
            IRLocal(
                name="_pc",
                type=IRType(cpp="int", fortran="integer", is_integer=True),
            )
        )


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
            parent_module=sub.parent_module,
        )
        entry.body, used_dispatch = structure_gotos(entry.body)
        if used_dispatch or _references_name(entry.body, "_pc"):
            entry.locals.append(
                IRLocal(
                    name="_pc",
                    type=IRType(cpp="int", fortran="integer", is_integer=True),
                )
            )
        _separate_parameters(entry, list(marker.arg_names), node)
        if sub.kind == "function":
            add_result_locals(entry, marker.name)
            _lift_function_return(entry, None, node)
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
        return IRType(cpp=_INT_KIND_CPP.get(_first_int(arg), "std::int32_t"),
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
        # selector is the length, possibly kind-suffixed (``14_8``).
        length = _first_int(arg.split(",")[0]) if arg else None
        if length is not None:
            return IRType(
                cpp=f"fortran::FortranString<{length}>",
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
    """Full ``fortran::Array`` IRTypes for names flang resolved with a
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
        cpp=f"fortran::Array<{element_type.cpp}, {rank}>",
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
    """Build a ``fortran::Array<T, Rank>`` IRType from a resolved symbol's
    constant shape (per-dimension inclusive ``(lower, upper)`` bounds)."""
    lowers = [lo for lo, _ in dims]
    extents = [hi - lo + 1 for lo, hi in dims]
    rank = len(dims)
    has_explicit_lower = any(lo != 1 for lo in lowers)
    return IRType(
        cpp=f"fortran::Array<{element_type.cpp}, {rank}>",
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
                var = obj.first_child("Variable")
                expr = _lower_expression(var) if var is not None else None
                if not isinstance(expr, IRName):
                    continue
                if expr.name in arrays:
                    rest = values[vi:]
                    out.append(
                        IRAssignment(
                            target=expr,
                            value=IRArrayConstructor(elements=tuple(rest)),
                        )
                    )
                    vi = len(values)
                elif vi < len(values):
                    out.append(IRAssignment(target=expr, value=values[vi]))
                    vi += 1
    return out


def _lower_data_value(value_node: Node) -> list[IRExpr]:
    """Lower one ``DataStmtValue`` to its constant(s).

    A ``DataStmtRepeat`` child (``3*7``) repeats the constant that many
    times, so this returns a list."""
    dc = value_node.first_child("DataStmtConstant")
    if dc is None:
        return [IRRaw("0")]
    inner = next(iter(dc.children), None)
    val = _lower_expression(inner) if inner is not None else IRRaw("0")
    count = 1
    repeat = value_node.first_child("DataStmtRepeat")
    if repeat is not None:
        lit = repeat.find_first("IntLiteralConstant")
        if lit is not None and lit.fortran:
            try:
                count = int(lit.fortran.split("_")[0])
            except ValueError:
                count = 1
    return [val] * count


def _lower_use_statements(spec_part: Node) -> list[str]:
    """Collect the module names imported by ``use`` statements."""
    out: list[str] = []
    for use in spec_part.find_all("UseStmt"):
        name = use.find_first("Name")
        if name is not None and name.fortran:
            out.append(_safe_name(name.fortran))
    return out


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
    decls = [loc for loc in out if loc.name not in param_names]
    # Statement functions become generic lambdas, declared last so they can
    # capture the locals they reference.
    return params + decls + _lower_statement_functions(spec_part)


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

    is_parameter = False
    is_save = False
    is_optional = False
    is_pointer = False
    intent: Literal["in", "out", "inout"] | None = None
    shared_array_spec: Node | None = None
    for attr in decl.find_all("AttrSpec"):
        # AttrSpec wraps the specific attribute child node — e.g.
        # ``Parameter``, ``Save``, ``IntentSpec``, ``ArraySpec``, etc.
        for child in attr.children:
            if child.kind == "Parameter":
                is_parameter = True
            elif child.kind == "Save":
                is_save = True
            elif child.kind == "Optional":
                is_optional = True
            elif child.kind == "Pointer":
                is_pointer = True
            elif child.kind == "External":
                # ``real, external :: f`` declares that ``f`` is a
                # function, not a variable — it's called via its
                # prototype, so emit no local (a local would shadow it).
                return []
            # ``Target`` needs no C++ analogue (any object is addressable).
            elif child.kind == "IntentSpec":
                intent = _extract_intent(child)
            elif child.kind == "ArraySpec":
                # ``dimension(...)`` applies to every EntityDecl that
                # doesn't carry its own ArraySpec.
                shared_array_spec = child

    out: list[IRLocal] = []
    for entity in decl.children:
        if entity.kind != "EntityDecl":
            continue
        name_node = entity.first_child("Name")
        if name_node is None or not name_node.fortran:
            continue
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
            )
        )
    return out


def _make_array_type(
    element_type: IRType, array_spec: Node, *, is_pointer: bool = False
) -> IRType:
    """Wrap ``element_type`` in ``fortran::Array<T, Rank>`` with
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
            cont = "fortran::ArrayRef" if is_pointer else "fortran::Array"
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
            # ``a(m, n, *)`` — an assumed-size spec that *wraps* the leading
            # explicit dimensions plus the trailing assumed one, so its rank
            # is the number of those dimension children (not one).  Caller
            # sized -> unknown extents.
            ndims = max(
                1,
                sum(
                    1
                    for c in shape.children
                    if c.kind in ("ExplicitShapeSpec", "AssumedImpliedSpec")
                ),
            )
            for _ in range(ndims):
                extents.append("/* assumed-size */ 0")
                lowers.append("1")
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
            # per ``AssumedImpliedSpec``.  The extent is unknown (caller
            # sized), so as a dummy it lowers to an ``ArrayRef`` of the
            # right rank; only the rank matters here.
            ndims = max(1, sum(1 for _ in shape.find_all("AssumedImpliedSpec")))
            for _ in range(ndims):
                extents.append("/* assumed-size */ 0")
                lowers.append("1")
            all_static = False
    rank = len(extents)
    return IRType(
        cpp=f"fortran::Array<{element_type.cpp}, {rank}>",
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
    pass the *extent* to ``fortran::Array``, but keep the lower bound
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
        "fortran::sum", "fortran::product", "fortran::maxval",
        "fortran::minval", "fortran::count", "fortran::any",
        "fortran::all", "fortran::dot_product", "fortran::size",
        "fortran::lbound", "fortran::ubound",
        "fortran::matmul", "fortran::transpose",
        "fortran::maxloc", "fortran::minloc",
        "fortran::pack", "fortran::cshift",
    }
)

# Intrinsics that return a whole array.  ``c = matmul(a, b)`` must NOT
# be expanded into an element loop (you can't index the call result);
# it stays a move-assignment of the returned Array.
_ARRAY_RETURNING: frozenset[str] = frozenset(
    {"fortran::matmul", "fortran::transpose", "fortran::reshape",
     "fortran::pack", "fortran::cshift", "fortran::eoshift",
     "fortran::spread"}
)


def _expand_array_assignments(sub: IRSubprogram) -> None:
    """Expand whole-array assignments (``a = b + c``) into explicit
    element loops, indexing the array operands and leaving scalars and
    whole-array (reduction) calls alone.

    Generated code reads like a hand-written loop nest and allocates no
    temporaries.  Array sections (``a(1:5) = ...``) are not handled
    here yet.
    """
    arrays = {loc.name: loc.type for loc in sub.locals if loc.type.is_array}
    if not arrays:
        return
    array_names = set(arrays)
    counter = [0]

    def expand(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRWhere):
            return _where_loop(stmt, arrays, array_names, counter)
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


def _unsupported_stmt(note: str) -> IRStatement:
    return IRUnsupported(kind="WHERE", source_text="", note=note)


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
                    op="/", lhs=IRBinaryOp(op="-", lhs=upper, rhs=lower), rhs=st
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
    return IRPrint(
        items=_lower_io_items(node, "OutputItem", "OutputImpliedDo"),
        format=_extract_format(node),
    )


def _lower_io_items(
    node: Node, item_kind: str, implied_kind: str
) -> list[IRExpr]:
    """Lower a print/read item list, handling implied-do items."""
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
                items.append(_lower_expression(expr))
        elif sub.kind == implied_kind:
            items.append(_lower_io_implied_do(sub, item_kind, implied_kind))
    return items


def _lower_io_implied_do(
    node: Node, item_kind: str, implied_kind: str
) -> IRExpr:
    lb = node.find_first("LoopBounds")
    var, lo, hi, step = _lower_loop_bounds(lb)
    inner = _lower_io_items(node, item_kind, implied_kind)
    return IRImpliedDo(
        var=var, lower=lo, upper=hi, step=step, items=tuple(inner)
    )


def _lower_write(node: Node) -> IRPrint:
    """Lower ``write(unit, fmt) items``.

    The unit selects the stream: ``*`` / ``6`` -> std::cout, ``0`` ->
    std::cerr.  Other (file) units are a TODO; we default to cout.
    """
    items: list[IRExpr] = []
    for sub in node.children:
        if sub.kind == "OutputItem":
            expr = sub.find_first("Expr")
            if expr is not None:
                items.append(_lower_expression(expr))
    io_unit = node.first_child("IoUnit")
    internal = _internal_file_unit(io_unit)
    stream = _stream_for_unit(io_unit)
    return IRPrint(
        items=items,
        stream=stream,
        format=_extract_format(node),
        internal_unit=internal,
    )


def _lower_read(node: Node) -> IRRead:
    """Lower ``read *, items`` / ``read(unit, fmt) items``.

    Only list-directed input is handled; the items become a ``>>``
    chain on the stream (``*`` / ``5`` -> std::cin).
    """
    items = _lower_io_items(node, "InputItem", "InputImpliedDo")
    io_unit = node.first_child("IoUnit")
    internal = _internal_file_unit(io_unit)
    stream = _input_stream_for_unit(io_unit)
    return IRRead(items=items, stream=stream, internal_unit=internal)


def _lower_open(node: Node) -> IRStatement:
    """``open(unit=u, file=f, status=s)`` -> ``_units.open(u, f, s)``."""
    unit: IRExpr | None = None
    file: IRExpr | None = None
    status: IRExpr | None = None
    for cs in node.children_of_kind("ConnectSpec"):
        if cs.first_child("FileUnitNumber") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                unit = _lower_expression(e)
        elif cs.first_child("StatusExpr") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                status = _lower_expression(e)
        elif cs.first_child("Scalar") is not None:
            e = cs.find_first("Expr")
            if e is not None:
                file = _lower_expression(e)
    args: list[IRExpr] = [unit if unit is not None else IRRaw("0")]
    if file is not None or status is not None:
        args.append(file if file is not None else IRRaw('""sv'))
    if status is not None:
        args.append(status)
    return IRCall(callee="_units.open", args=args)


def _lower_close(node: Node) -> IRStatement:
    """``close(u)`` -> ``_units.close(u)``."""
    fun = node.find_first("FileUnitNumber")
    e = fun.find_first("Expr") if fun is not None else None
    unit = _lower_expression(e) if e is not None else IRRaw("0")
    return IRCall(callee="_units.close", args=[unit])


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
    lit = io_unit.find_first("IntLiteralConstant")
    if lit is not None and lit.fortran:
        return lit.fortran.split("_")[0]
    # A bare scalar variable used as the unit (``write(lun, ...)``).
    if io_unit.find_first("Add") is None and io_unit.find_first("Multiply") is None:
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
    if obj_node is not None:
        # The allocated object may be a derived-type component
        # (``allocate(subset%beta(...))``) — keep the full access path.
        sc = obj_node.first_child("StructureComponent")
        if sc is not None:
            path = _access_path(_lower_structure_component(sc))
            if path is not None:
                obj = path
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
        obj=obj, extents=extents, lowers=lowers if has_lower else []
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


def _extract_format(node: Node) -> str | None:
    """Return the format string for a Print/Write, or None for the
    list-directed (``*``) form."""
    fmt = node.first_child("Format")
    if fmt is None:
        return None
    if fmt.first_child("Star") is not None:
        return None
    # A label reference (``write(u, 100)``) -> the FORMAT statement's spec.
    label = fmt.first_child("uint64_t")
    if label is not None and label.fortran:
        try:
            return _FORMAT_LABELS.get(int(label.fortran))
        except ValueError:
            pass
    # Otherwise an inline character literal; pull its body.
    for s in fmt.walk():
        if s.kind == "string" and s.fortran is not None:
            return s.fortran
    return None


# Fortran intrinsic *subroutines* (invoked with CALL) that map to a
# ``fortran::`` runtime helper rather than a user-defined function.
_INTRINSIC_SUBROUTINE_MAP: dict[str, str] = {
    "cpu_time": "fortran::cpu_time",
    "system_clock": "fortran::system_clock",
}


def _lower_call(node: Node) -> IRCall:
    call = node.first_child("Call") or node
    callee, leading = _resolve_callee(call)
    args = _resolve_call_args(callee, leading, call)
    intrinsic = _INTRINSIC_SUBROUTINE_MAP.get(callee)
    if intrinsic is not None and not leading:
        return IRCall(callee=intrinsic, args=args)
    return IRCall(callee=_safe_name(callee), args=args)


def _lower_if_construct(node: Node) -> IRIf:
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
    return IRDo(var=var, lower=lo, upper=hi, step=step, body=body)


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
        # Range form: children may include lower and/or upper bounds.
        exprs = list(range_node.find_all("Expr"))
        lo = _lower_expression(exprs[0]) if exprs else None
        hi = _lower_expression(exprs[1]) if len(exprs) > 1 else None
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
        src = (rng.source.text or "").strip() if rng.source else ""
        if len(scalars) >= 2:
            lo = _lower_expression(scalars[0])
            hi = _lower_expression(scalars[1])
        elif len(scalars) == 1:
            # One bound omitted; the colon's position says which.
            if src.startswith(":"):
                hi = _lower_expression(scalars[0])
            else:
                lo = _lower_expression(scalars[0])
    if lo is None:
        lo = IRLiteral(cpp_text="1")
    if hi is None:
        # Open upper bound ``s(lo:)`` -> to the end.  ``fortran::len``
        # works whether the base is a FortranString, a CharRef, or a
        # character-array element (all view as a string).
        hi = IRFunctionCall(callee="fortran::len", args=(base,))
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

    fortran::Array overloads ``operator()`` with exactly the same
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
    """Lower ``lo:hi:stride`` (any part optional)."""
    # The triplet's direct children are the present bound expressions,
    # in order: it may have lower, upper, and/or stride.  flang wraps
    # each in a Scalar/Integer; we lower each present Expr.  Missing
    # parts default to the array's bounds at emit/expansion time.
    parts: list[IRExpr | None] = [None, None, None]
    # Each bound is under a direct child that contains an Expr.
    bound_children = [
        c for c in triplet.children if c.find_first("Expr") is not None
    ]
    # SubscriptTriplet stores (lower?, upper?, stride?) — but with
    # optionals collapsed, we can't always tell which is which by
    # position alone.  Use the source text to disambiguate the common
    # forms; default to filling lower, then upper, then stride.
    for i, c in enumerate(bound_children[:3]):
        expr = c.find_first("Expr")
        parts[i] = _lower_expression(expr) if expr is not None else None
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
        return IRLiteral(cpp_text=value, cpp_type="std::int32_t")
    return IRLiteral(cpp_text=f"{value}LL", cpp_type="std::int64_t")


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
    body_node = node.first_child("bool")
    if body_node and body_node.fortran is not None:
        return IRLiteral(cpp_text="true" if body_node.fortran == "true" else "false")
    raw = (node.source.text if node.source else "").lower().strip(". ")
    return IRLiteral(cpp_text="true" if "t" in raw[:1] else "false")


# Fortran intrinsics that map directly to a name in <cmath> / std::.
# Anything not in this table is emitted as a plain call; the user's
# own functions therefore "just work" as long as they have a C++
# definition (typically a translated sibling subprogram).
_INTRINSIC_MAP: dict[str, str] = {
    # Elemental math -> fortran:: overloads that map over arrays as well
    # as scalars (intrinsics.hpp); scalar calls delegate to std::.
    "sqrt": "fortran::sqrt", "abs": "fortran::abs", "exp": "fortran::exp",
    "log": "fortran::log", "log10": "fortran::log10",
    "sin": "fortran::sin", "cos": "fortran::cos", "tan": "fortran::tan",
    "asin": "fortran::asin", "acos": "fortran::acos", "atan": "fortran::atan",
    "atan2": "std::atan2", "sinh": "fortran::sinh", "cosh": "fortran::cosh",
    "tanh": "fortran::tanh", "floor": "std::floor", "ceiling": "std::ceil",
    "min": "std::min", "max": "std::max",
    # Bit-manipulation intrinsics.
    "iand": "fortran::iand", "ior": "fortran::ior", "ieor": "fortran::ieor",
    "ishft": "fortran::ishft", "btest": "fortran::btest",
    "ibset": "fortran::ibset", "ibclr": "fortran::ibclr",
    # Numeric inquiry intrinsics.
    "huge": "fortran::huge", "tiny": "fortran::tiny",
    "epsilon": "fortran::epsilon", "kind": "fortran::kind",
    "bit_size": "fortran::bit_size", "precision": "fortran::precision",
    "radix": "fortran::radix", "digits": "fortran::digits",
    # Character <-> integer intrinsics.
    "achar": "fortran::achar", "char": "fortran::achar",
    "iachar": "fortran::ichar", "ichar": "fortran::ichar",
    "mod": "fortran::mod",       # generic: integer % or std::fmod
    "amod": "fortran::mod", "dmod": "fortran::mod",  # real/double specifics
    "modulo": "fortran::modulo",  # remainder with sign of divisor
    "merge": "fortran::merge",
    "sign": "std::copysign", "dsign": "std::copysign",
    # FORTRAN 77 type-specific intrinsic spellings -> the generic forms.
    "alog": "fortran::log", "dlog": "fortran::log",
    "alog10": "fortran::log10", "dlog10": "fortran::log10",
    "dsqrt": "fortran::sqrt", "dexp": "fortran::exp",
    "dabs": "fortran::abs", "iabs": "fortran::abs",
    "dsin": "fortran::sin", "dcos": "fortran::cos", "dtan": "fortran::tan",
    "dasin": "fortran::asin", "dacos": "fortran::acos", "datan": "fortran::atan",
    "datan2": "std::atan2", "dsinh": "fortran::sinh",
    "dcosh": "fortran::cosh", "dtanh": "fortran::tanh",
    "amax1": "std::max", "dmax1": "std::max", "max0": "std::max",
    "amax0": "std::max",
    "amin1": "std::min", "dmin1": "std::min", "min0": "std::min",
    "amin0": "std::min",
    "dnint": "fortran::anint", "idnint": "fortran::nint",
    # Array intrinsics -> fortran:: runtime helpers (intrinsics.hpp).
    "size": "fortran::size", "lbound": "fortran::lbound",
    "ubound": "fortran::ubound", "sum": "fortran::sum",
    "product": "fortran::product", "maxval": "fortran::maxval",
    "minval": "fortran::minval", "count": "fortran::count",
    "any": "fortran::any", "all": "fortran::all",
    "dot_product": "fortran::dot_product",
    "matmul": "fortran::matmul", "transpose": "fortran::transpose",
    "maxloc": "fortran::maxloc", "minloc": "fortran::minloc",
    "pack": "fortran::pack", "cshift": "fortran::cshift",
    "eoshift": "fortran::eoshift", "spread": "fortran::spread",
    # Character intrinsics.
    "trim": "fortran::trim", "len": "fortran::len",
    "len_trim": "fortran::len_trim", "index": "fortran::index",
    "adjustl": "fortran::adjustl", "adjustr": "fortran::adjustr",
    "repeat": "fortran::repeat", "scan": "fortran::scan",
    "verify": "fortran::verify",
    # Rounding / truncating conversions (plain int/real/dble are casts,
    # handled separately in _lower_conversion_intrinsic).
    "nint": "fortran::nint", "aint": "fortran::aint",
    "anint": "fortran::anint", "dint": "fortran::aint",
    # Lexical (collating-sequence) string comparisons.
    "llt": "fortran::llt", "lle": "fortran::lle",
    "lgt": "fortran::lgt", "lge": "fortran::lge",
}


# Kind-dependent numeric conversion intrinsics -> C++ casts.  The
# target C++ type depends on the (optional) kind argument.
_INT_KIND_CPP = {
    None: "std::int32_t", 1: "std::int8_t", 2: "std::int16_t",
    4: "std::int32_t", 8: "std::int64_t",
}
_REAL_KIND_CPP = {None: "float", 4: "float", 8: "double"}


def _lower_function_reference(node: Node) -> IRExpr:
    call = node.first_child("Call") or node
    callee, leading = _resolve_callee(call)
    args = _resolve_call_args(callee, leading, call)

    # present(x) -> x.has_value() (x is a std::optional param).  Use the
    # raw optional name; the deref pass won't touch this IRRaw.
    if callee == "present" and len(args) == 1 and isinstance(args[0], IRName):
        return IRRaw(f"({args[0].name}.has_value())")

    # associated(p) -> fortran::associated(p) using the raw pointer name
    # (not the deref'd value), overloaded for T* and ArrayRef.
    if callee == "associated" and len(args) == 1 and isinstance(args[0], IRName):
        return IRRaw(f"fortran::associated({args[0].name})")

    # Conversion intrinsics become static_casts whose target type
    # depends on the kind argument.
    conv = _lower_conversion_intrinsic(callee, args)
    if conv is not None:
        return conv

    # reshape(source, [d1, d2, ...]) -> fortran::reshape(source, d1, d2, ...)
    # so the result rank is deduced from the (literal) shape's length.
    if callee == "reshape" and len(args) >= 2 and isinstance(
        args[1], IRArrayConstructor
    ):
        flat = (args[0], *args[1].elements)
        return IRFunctionCall(callee="fortran::reshape", args=flat)

    # Not an intrinsic -> a user function; safe-name it to match the
    # (safe-named) subprogram definition.
    cpp_callee = _INTRINSIC_MAP.get(callee, _safe_name(callee))
    return IRFunctionCall(callee=cpp_callee, args=tuple(args))


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
        return IRCast(cpp_type=_INT_KIND_CPP.get(kind, "std::int32_t"),
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
    return [expr for _, expr in _lower_actual_arg_pairs(call)]


def _lower_actual_arg_pairs(call: Node) -> list[tuple[str | None, IRExpr]]:
    """Like :func:`_lower_actual_args` but pairs each argument with its
    keyword name (or ``None`` for a positional argument)."""
    pairs: list[tuple[str | None, IRExpr]] = []
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
            pairs.append((kw, _lower_expression(expr)))
    return pairs


def _resolve_call_args(callee: str, leading: list[IRExpr], call: Node) -> list[IRExpr]:
    """Build the final positional argument list for a call, applying
    keyword-argument reordering against the callee's known signature.

    ``leading`` holds any synthetic leading arguments (the passed object
    of a type-bound call); the callee's first dummy corresponds to it and
    is dropped before matching the explicit keyword arguments.
    """
    pairs = _lower_actual_arg_pairs(call)
    dummies = _SIGNATURES.get(callee, [])
    if leading and dummies:
        dummies = dummies[1:]
    return leading + _reorder_keyword_args(pairs, dummies)


def _reorder_keyword_args(
    pairs: list[tuple[str | None, IRExpr]], dummies: list[tuple[str, bool]]
) -> list[IRExpr]:
    """Reorder ``(keyword, expr)`` pairs into positional order using the
    callee's ordered ``(dummy_name, is_optional)`` list.  Positional args
    fill slots left to right; keyword args drop into their named slot.

    A gap left by an omitted OPTIONAL argument is filled with
    ``std::nullopt`` so the remaining positional arguments stay aligned;
    a *trailing* run of omitted optionals is dropped entirely (the C++
    default argument supplies ``std::nullopt``).  With no keywords (or an
    unknown callee) the original positional order is preserved."""
    if not any(kw is not None for kw, _ in pairs) or not dummies:
        return [expr for _, expr in pairs]
    names = [n for n, _ in dummies]
    slots: list[IRExpr | None] = [None] * len(dummies)
    extra: list[IRExpr] = []
    pos = 0
    for kw, expr in pairs:
        if kw is None:
            if pos < len(slots):
                slots[pos] = expr
            else:
                extra.append(expr)
            pos += 1
        elif kw in names:
            slots[names.index(kw)] = expr
        else:
            extra.append(expr)
    # Drop the trailing run of unfilled optional slots (C++ defaults them).
    last = len(slots)
    while last > 0 and slots[last - 1] is None and dummies[last - 1][1]:
        last -= 1
    result: list[IRExpr] = []
    for i in range(last):
        if slots[i] is not None:
            result.append(slots[i])  # type: ignore[arg-type]
        elif dummies[i][1]:
            result.append(IRRaw("std::nullopt"))
        # An unfilled non-optional slot can't happen for valid Fortran;
        # skip it rather than emit a bogus argument.
    return result + extra


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
    "Power": "**",         # placeholder — lowered to std::pow
    "Concat": "//",        # placeholder — lowered to fortran::concat
    "DefinedBinary": "?",  # user-defined op — TODO, emit as call
}

_UNARY_OP_MAP: dict[str, str] = {
    "Negate": "-",
    "UnaryPlus": "+",
    "NOT": "!",
    "Parentheses": "()",   # special-cased in the emitter
    "DefinedUnary": "?",   # user-defined op — TODO
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
                return IRFunctionCall(callee="fortran::concat", args=(lhs, rhs))
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


def _expr_raw(node: Node) -> IRRaw:
    src = node.source.text if node.source else node.kind
    return IRRaw(text=f"/* TODO: {node.kind} */ {src}")


def _unsupported(
    node: Node, *, kind: str, leading: list[Comment] | None = None
) -> IRUnsupported:
    src = node.source.text if node.source else ""
    return IRUnsupported(
        kind=kind,
        source_text=src,
        leading_comments=leading or [],
    )


def _fortran_char_literal_body(raw: str) -> str:
    """Strip surrounding Fortran quotes and undo doubled-quote escapes."""
    raw = raw.strip()
    if len(raw) < 2 or raw[0] not in ("'", '"') or raw[-1] != raw[0]:
        return raw
    q = raw[0]
    return raw[1:-1].replace(q + q, q)
