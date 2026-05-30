"""Post-lowering pass that plumbs persistent / scratch state through calls.

Fortran has several kinds of state that a naive translation would turn
into globals (unsafe for threads) or per-call stack/heap objects (a
stack-overflow risk or a per-call allocation cost).  Following decision
D2.b, this pass turns every such category into an explicit,
caller-owned struct threaded through the call graph — no statics, no
thread_local — so independent program instances stay isolated and
large buffers are allocated exactly once.

Categories handled, each becoming a struct + a reference parameter:

  * **module variables**  -> ``<Name>Module`` (shared program state)
  * **common blocks**     -> ``<Name>Common`` (shared program state)
  * **SAVE locals**       -> ``<Routine>Save``  (persists across calls)
  * **fixed-size local arrays** -> ``<Routine>Workspace``
        (scratch, allocated once and reused — fixes the
         stack-overflow-vs-per-call-allocation dilemma).  Only applied
         to non-recursive routines, since a single shared workspace
         can't back two simultaneously-active invocations.

Rather than rewrite body references into ``param.field`` accesses, the
pass records ``auto& field = param.field;`` bindings (emitted at the
top of the body) so the body keeps referring to variables by their
original names and stays readable.

Each struct is owned (allocated) by the top of its call chain — the
main program, typically — and forwarded down by reference.
"""

from __future__ import annotations

import re

from .ir import (
    IRCall,
    IRExpr,
    IRFunctionCall,
    IRLocal,
    IRName,
    IRPrint,
    IRRaw,
    IRRead,
    IRSection,
    IRStateBinding,
    IRStateParam,
    IRStateStruct,
    IRStatement,
    IRSubprogram,
    IRTranslationUnit,
    IRType,
)
from .lowering import _render_expr_inline
from .transform import map_expr, map_statement


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plumb_state(tu: IRTranslationUnit) -> None:
    """Run all state-plumbing transformations on ``tu`` (in place)."""
    _build_module_structs(tu)
    _build_common_structs(tu)
    _build_save_structs(tu)
    _build_workspaces(tu)
    _build_unit_state(tu)
    _propagate_state_parameters(tu)
    _rewrite_call_sites(tu)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _camelcase(name: str) -> str:
    parts = name.replace("-", "_").split("_")
    return "".join(p.capitalize() if p else "" for p in parts)


def _collect_names(body: list[IRStatement]) -> set[str]:
    """Every name referenced anywhere in ``body``.

    Besides plain ``IRName`` references this includes ``IRFunctionCall``
    callees, because an array element reference (``a(i)``) lowers to a
    call-shaped node whose callee is the array's name — so array-valued
    module variables would otherwise look unreferenced and never get
    threaded/bound.  Spurious function names (``std::sin`` etc.) are
    harmless: they won't match a module variable's name.
    """
    names: set[str] = set()

    def note(expr: IRExpr) -> IRExpr:
        if isinstance(expr, IRName):
            names.add(expr.name)
        elif isinstance(expr, IRFunctionCall):
            # An array/component access (``a(i)``, ``v.c(i)``) lowers to a
            # call-shaped node whose callee is a dotted access path; record
            # the path *and* its base variable (``v`` in ``v.c``) so a
            # module variable referenced only through a component is still
            # detected.
            names.add(expr.callee)
            names.add(expr.callee.split(".", 1)[0])
        elif isinstance(expr, IRSection):
            names.add(expr.array)
            names.add(expr.array.split(".", 1)[0])
        return expr

    for stmt in body:
        map_statement(stmt, on_expr=lambda e: map_expr(e, note))
    return names


def _attach_state(
    sub: IRSubprogram,
    *,
    struct_type: str,
    param_name: str,
    owned_by: str,
    bound_fields: list[str | tuple[str, str]],
) -> None:
    """Give ``sub`` access to a state struct and bind the fields it uses.

    A non-main routine receives the struct as a reference parameter; the
    main program owns the instance as a value-initialized local (it
    can't take parameters).  Either way we emit ``auto& f = p.f;``
    bindings so the body references the fields by name.  A field may be
    bound under a *different* local name (``auto& local = p.field;``) by
    passing a ``(local_name, field_name)`` pair — needed for common
    blocks whose members are spelled differently in different routines.
    """
    if sub.kind == "main":
        if not any(loc.name == param_name for loc in sub.locals):
            sub.locals.insert(
                0,
                IRLocal(
                    name=param_name,
                    type=IRType(cpp=struct_type, fortran=struct_type),
                    initializer=IRRaw("{}"),
                ),
            )
    else:
        if not any(sp.struct_type == struct_type for sp in sub.state_params):
            sub.state_params.append(
                IRStateParam(
                    name=param_name, struct_type=struct_type, owned_by=owned_by
                )
            )
    for entry in bound_fields:
        local_name, field_name = (entry, entry) if isinstance(entry, str) else entry
        if not any(
            b.name == local_name and b.param == param_name
            for b in sub.state_bindings
        ):
            sub.state_bindings.append(
                IRStateBinding(name=local_name, param=param_name, field=field_name)
            )


# ---------------------------------------------------------------------------
# Module variables
# ---------------------------------------------------------------------------


def _build_module_structs(tu: IRTranslationUnit) -> None:
    modules = [m for m in tu.modules if m.variables]
    if not modules:
        return
    module_by_name = {m.fortran_name: m for m in modules}

    for sub in tu.subprograms:
        in_scope: list[str] = []
        if sub.parent_module in module_by_name:
            in_scope.append(sub.parent_module)  # type: ignore[arg-type]
        for m in sub.used_modules:
            if m in module_by_name and m not in in_scope:
                in_scope.append(m)
        if not in_scope:
            continue

        local_names = {loc.name for loc in sub.locals} | {
            p.name for p in sub.parameters
        }
        referenced = _collect_names(sub.body)

        for mod_name in in_scope:
            module = module_by_name[mod_name]
            # PARAMETERs are emitted as free compile-time constants, so
            # they need no threaded instance — only mutable variables do.
            touched = [
                v
                for v in module.variables
                if v.name in referenced
                and v.name not in local_names
                and not v.is_parameter
            ]
            if not touched:
                continue
            _attach_state(
                sub,
                struct_type=module.cpp_type,
                param_name=mod_name + "_module",
                owned_by="__module_" + mod_name,
                bound_fields=[v.name for v in touched],
            )


# ---------------------------------------------------------------------------
# Common blocks
# ---------------------------------------------------------------------------


def _build_common_structs(tu: IRTranslationUnit) -> None:
    """A common block is shared storage declared (re-)independently in
    each routine that uses it.  Routines may spell its slots with
    different names and even tile them differently (one routine's
    ``dl(16)`` is another's twelve scalars ``tlb,s,...``), so we model the
    block by the *union* of the distinctly-named members across all
    routines, typed from wherever each name is declared.  Crucially, a
    routine binds (and has dropped from its locals) only the members *it
    itself* declared in the block — never the whole union — so a routine
    that never put name ``x`` in the block keeps its own local ``x``
    instead of having it shadowed by another routine's common member."""
    block_members: dict[str, list[str]] = {}
    block_member_types: dict[str, dict[str, IRType]] = {}
    # Per (sub index, block), this routine's own declared members.
    own: dict[int, dict[str, list[str]]] = {}
    for idx, sub in enumerate(tu.subprograms):
        local_types = {loc.name: loc.type for loc in sub.locals}
        for use in sub.common_uses:
            members = block_members.setdefault(use.block_name, [])
            types = block_member_types.setdefault(use.block_name, {})
            mine = own.setdefault(idx, {}).setdefault(use.block_name, [])
            for m in use.member_names:
                mine.append(m)
                if m not in members:
                    members.append(m)
                lt = local_types.get(m)
                if lt is not None and (
                    m not in types
                    # A CHARACTER declaration is authoritative: implicit
                    # typing never yields character, so a routine that
                    # spells a common slot CHARACTER pins its type over a
                    # (possibly implicit) integer/real view elsewhere.
                    or (lt.is_character and not types[m].is_character)
                ):
                    types[m] = lt
    if not block_members:
        return

    struct_for_block: dict[str, IRStateStruct] = {}
    for block_name, members in block_members.items():
        types = block_member_types.get(block_name, {})
        fields = [
            IRLocal(
                name=m,
                type=types.get(
                    m, IRType(cpp="/* TODO: type */ double", fortran="?")
                ),
            )
            for m in members
        ]
        struct_for_block[block_name] = IRStateStruct(
            cpp_type=_common_struct_name(block_name), fields=fields
        )
    for struct in struct_for_block.values():
        tu.common_structs.append(struct)

    for idx, sub in enumerate(tu.subprograms):
        param_names = {p.name for p in sub.parameters}
        for block_name, members in own.get(idx, {}).items():
            struct = struct_for_block[block_name]
            # This routine's own common members are also declared as
            # locals in Fortran; drop those (only the ones *this* routine
            # put in the block — a like-named local elsewhere stays).
            member_set = set(members)
            sub.locals = [
                loc for loc in sub.locals if loc.name not in member_set
            ]
            # A member shadowed by a dummy argument of the same name can't
            # reference the common entity here, so skip it.
            _attach_state(
                sub,
                struct_type=struct.cpp_type,
                param_name=_common_param_name(block_name),
                owned_by="__common_" + block_name,
                bound_fields=[m for m in members if m not in param_names],
            )


def _common_struct_name(block_name: str) -> str:
    return "BlankCommon" if not block_name else _camelcase(block_name) + "Common"


def _common_param_name(block_name: str) -> str:
    return "blank_common" if not block_name else block_name + "_common"


# ---------------------------------------------------------------------------
# SAVE locals
# ---------------------------------------------------------------------------


_IDENT_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b")


def _hoisted_parameters(
    save_locals: list[IRLocal], all_locals: list[IRLocal]
) -> list[IRLocal]:
    """Subprogram-local PARAMETER constants the SAVE locals' array bounds
    reference — promote them as ``static constexpr`` members of the SAVE
    struct so the field initializer ``Array<T,N> a{{maxsiz, ...}}`` resolves
    (the function-body ``constexpr`` declaration is invisible at struct
    scope).  Follows the parameter chain transitively (``max2 = maxsiz *
    maxsiz`` brings in ``maxsiz`` too)."""
    params = {loc.name: loc for loc in all_locals if loc.is_parameter}
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name not in params or name in seen:
            return
        seen.add(name)
        init = params[name].initializer
        if init is not None:
            for tok in _IDENT_RE.findall(_render_expr_inline(init)):
                visit(tok)
        order.append(name)

    for loc in save_locals:
        for expr in loc.type.array_extent_exprs or ():
            for tok in _IDENT_RE.findall(expr):
                visit(tok)
        for expr in loc.type.array_lower_bound_exprs or ():
            for tok in _IDENT_RE.findall(expr):
                visit(tok)
    return [params[n] for n in order]


def _build_save_structs(tu: IRTranslationUnit) -> None:
    for sub in tu.subprograms:
        save_locals = [loc for loc in sub.locals if loc.is_save]
        if not save_locals:
            continue
        hoisted = _hoisted_parameters(save_locals, sub.locals)
        struct_type = _camelcase(sub.display_name) + "Save"
        # Hoisted PARAMETER members come first so the SAVE field
        # initializers below them can name those constants unqualified.
        sub.save_struct = IRStateStruct(
            cpp_type=struct_type, fields=hoisted + save_locals
        )
        sub.locals = [loc for loc in sub.locals if not loc.is_save]
        _attach_state(
            sub,
            struct_type=struct_type,
            param_name=sub.name + "_save",
            owned_by=sub.name,
            bound_fields=[loc.name for loc in save_locals],
        )


# ---------------------------------------------------------------------------
# Fixed-size local arrays -> per-routine workspace (allocated once)
# ---------------------------------------------------------------------------


def _build_workspaces(tu: IRTranslationUnit) -> None:
    recursive = _recursive_routines(tu)
    for sub in tu.subprograms:
        if sub.kind == "main":
            # The main program runs once, so its arrays are already
            # allocated once as plain locals — a workspace would just
            # add noise with no per-call-allocation benefit.
            continue
        if sub.name in recursive:
            # A shared workspace can't back two active invocations of a
            # recursive routine; leave its arrays as per-call locals.
            continue
        hoist = [
            loc
            for loc in sub.locals
            if loc.type.is_array and loc.type.array_static and not loc.is_save
        ]
        if not hoist:
            continue
        hoist_names = {loc.name for loc in hoist}
        struct_type = _camelcase(sub.display_name) + "Workspace"
        sub.workspace = IRStateStruct(cpp_type=struct_type, fields=hoist)
        sub.locals = [loc for loc in sub.locals if loc.name not in hoist_names]
        _attach_state(
            sub,
            struct_type=struct_type,
            param_name=sub.name + "_workspace",
            owned_by=sub.name,
            bound_fields=[loc.name for loc in hoist],
        )


def _recursive_routines(tu: IRTranslationUnit) -> set[str]:
    """Names of routines that can (transitively) call themselves."""
    names = {s.name for s in tu.subprograms}
    adj: dict[str, set[str]] = {s.name: set() for s in tu.subprograms}
    for s in tu.subprograms:
        for callee in _callee_names(s.body):
            if callee in names:
                adj[s.name].add(callee)
    recursive: set[str] = set()
    for start in adj:
        seen: set[str] = set()
        stack = list(adj[start])
        while stack:
            n = stack.pop()
            if n == start:
                recursive.add(start)
                break
            if n in seen:
                continue
            seen.add(n)
            stack.extend(adj.get(n, ()))
    return recursive


# ---------------------------------------------------------------------------
# Connected file units (OPEN/CLOSE and unit-directed I/O)
# ---------------------------------------------------------------------------

_UNITS_TYPE = "fortran::io::Units"
_UNITS_PARAM = "_units"


def _build_unit_state(tu: IRTranslationUnit) -> None:
    """Thread a ``fortran::io::Units`` table into routines that OPEN/CLOSE
    a unit or do unit-directed (file / variable-unit) I/O."""
    for sub in tu.subprograms:
        if _uses_units(sub.body):
            _attach_state(
                sub,
                struct_type=_UNITS_TYPE,
                param_name=_UNITS_PARAM,
                owned_by="__units",
                bound_fields=[],
            )


def _uses_units(body: list[IRStatement]) -> bool:
    found = [False]

    def check(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRCall) and stmt.callee.startswith(_UNITS_PARAM + "."):
            found[0] = True
        elif isinstance(stmt, (IRPrint, IRRead)) and _UNITS_PARAM in stmt.stream:
            found[0] = True
        return stmt

    for stmt in body:
        map_statement(stmt, on_stmt=check)
    return found[0]


# ---------------------------------------------------------------------------
# Forward state parameters up call chains
# ---------------------------------------------------------------------------


def _procedure_actuals(
    caller: IRSubprogram, by_name: dict[str, IRSubprogram]
) -> set[str]:
    """Subprogram names ``caller`` passes as a *procedure* argument.

    Such an actual is wrapped in a state-capturing lambda at the call
    site, so ``caller`` must itself hold the passed routine's state to
    capture it — exactly as if it called the routine directly."""
    out: set[str] = set()

    def scan(callee_name: str, args) -> None:
        callee = by_name.get(callee_name)
        if callee is None:
            return
        for i, a in enumerate(args):
            if (
                i < len(callee.parameters)
                and callee.parameters[i].type.is_procedure
                and isinstance(a, IRName)
                and a.name in by_name
            ):
                out.add(a.name)

    def on_stmt(s: IRStatement) -> IRStatement:
        if isinstance(s, IRCall):
            scan(s.callee, s.args)
        return s

    def on_expr(e: IRExpr) -> IRExpr:
        if isinstance(e, IRFunctionCall):
            scan(e.callee, e.args)
        return e

    for s in caller.body:
        map_statement(s, on_stmt=on_stmt, on_expr=lambda e: map_expr(e, on_expr))
    return out


def _propagate_state_parameters(tu: IRTranslationUnit) -> None:
    by_name = {s.name: s for s in tu.subprograms}
    changed = True
    while changed:
        changed = False
        for caller in tu.subprograms:
            if caller.kind == "main":
                continue  # main owns instances locally (handled below)
            needed = set(
                _callee_names(caller.body, _shadowed_names(caller))
            ) | _procedure_actuals(caller, by_name)
            for callee_name in needed:
                callee = by_name.get(callee_name)
                if callee is None:
                    continue
                for sp in callee.state_params:
                    if any(
                        existing.struct_type == sp.struct_type
                        for existing in caller.state_params
                    ):
                        continue
                    caller.state_params.append(
                        IRStateParam(
                            name=sp.name,
                            struct_type=sp.struct_type,
                            owned_by=sp.owned_by,
                        )
                    )
                    changed = True


# ---------------------------------------------------------------------------
# At every call site, prepend the state arguments the callee expects
# ---------------------------------------------------------------------------


def _storage_pun(
    arg: IRExpr, actual_ty: IRType | None, param
) -> IRExpr | None:
    """Fortran storage association across mismatched types: an actual whose
    C++ type *cannot bind* to the dummy's reference/view (passing a
    ``DOUBLE PRECISION`` actual to an ``INTEGER`` dummy under an implicit
    interface, and the like).  Reinterpret the storage so the otherwise
    non-compiling call type-checks, matching F77 by-reference semantics.

    Returns the reinterpreting expression, or ``None`` when no pun is
    needed.  Gated on the *would-otherwise-fail* cases only — a const-ref or
    by-value dummy already converts implicitly, so those are left untouched
    and no currently-compiling call site changes."""
    if not isinstance(arg, IRName) or actual_ty is None:
        return None
    pty = param.type
    if pty.is_procedure or param.optional:
        return None

    def _is_arith(t: IRType) -> bool:
        return (t.is_integer or t.is_real) and t.cpp not in ("std::string_view",)

    # Scalar pun: only a non-const reference dummy (intent out/inout) fails
    # to bind a different arithmetic type; a const-ref/value dummy converts.
    if (
        not pty.is_array
        and not actual_ty.is_array
        and param.intent != "in"
        and _is_arith(pty)
        and _is_arith(actual_ty)
        and pty.cpp != actual_ty.cpp
    ):
        return IRRaw(text=f"fortran::storage_ref<{pty.cpp}>({arg.name})")

    # Array pun: a rank-1 element-type mismatch has no converting ctor (the
    # ArrayRef converting ctor only adds ``const``), so it can't bind.
    if (
        pty.is_array
        and actual_ty.is_array
        and pty.array_rank == 1
        and actual_ty.array_rank == 1
        and pty.element_type_cpp not in ("", "std::string_view")
        and actual_ty.element_type_cpp not in ("", "std::string_view")
        and pty.element_type_cpp != actual_ty.element_type_cpp
    ):
        return IRRaw(
            text=f"fortran::reinterpret_array<{pty.element_type_cpp}>({arg.name})"
        )
    return None


def _rewrite_call_sites(tu: IRTranslationUnit) -> None:
    by_name = {s.name: s for s in tu.subprograms}
    all_struct_types = (
        {sp.struct_type for s in tu.subprograms for sp in s.state_params}
        | {st.cpp_type for st in tu.common_structs}
        | {m.cpp_type for m in tu.modules}
        | {s.workspace.cpp_type for s in tu.subprograms if s.workspace}
        | {s.save_struct.cpp_type for s in tu.subprograms if s.save_struct}
    )
    for caller in tu.subprograms:
        caller_state_names = {
            sp.struct_type: sp.name for sp in caller.state_params
        }
        for loc in caller.locals:
            if loc.type.cpp in all_struct_types:
                caller_state_names.setdefault(loc.type.cpp, loc.name)
        local_state_instances: dict[str, str] = {}
        # Data names that shadow a like-named subprogram in this routine: a
        # ``name(...)`` using one is indexing/substring, not a call, so it
        # must not have state arguments prepended.
        shadowed = _shadowed_names(caller)
        # Resolved types of this caller's own data, for detecting a storage
        # pun (a type-mismatched actual that can't bind the dummy).
        caller_types = {p.name: p.type for p in caller.parameters}
        for loc in caller.locals:
            caller_types.setdefault(loc.name, loc.type)

        def state_args(callee_name: str) -> list[IRExpr] | None:
            """The state arguments to prepend at a call to ``callee_name``,
            or ``None`` if it takes none."""
            callee = by_name.get(callee_name)
            if callee is None or not callee.state_params or callee_name in shadowed:
                return None
            extra: list[IRExpr] = []
            for sp in callee.state_params:
                if sp.struct_type in caller_state_names:
                    nm = caller_state_names[sp.struct_type]
                else:
                    nm = local_state_instances.setdefault(sp.struct_type, sp.name)
                extra.append(IRName(name=nm, fortran=nm))
            return extra

        def proc_lambda(actual_name: str, arity: int) -> IRRaw:
            """A state-capturing *generic* lambda adapting ``actual_name`` (a
            procedure passed as an argument) to a dummy-procedure parameter.

            The receiving routine takes the callback as a deduced template
            type, so the lambda needs no fixed parameter signature: it accepts
            whatever the callee invokes it with (``auto&&...``) and forwards
            those after the captured state arguments.  A generic lambda is a
            concrete object with its own type, so even a higher-order routine
            (itself a template) can be passed this way — deduction latches
            onto the closure, not the un-instantiable template name."""
            sargs = state_args(actual_name) or []
            state = "".join(
                f"{e.name}, " for e in sargs if isinstance(e, IRName)
            )
            actual = by_name.get(actual_name)
            ret = "return " if (actual is not None and actual.kind == "function") else ""
            return IRRaw(
                f"[&](auto&&... _a) {{ {ret}{actual_name}("
                f"{state}std::forward<decltype(_a)>(_a)...); }}"
            )

        def wrap_proc_args(callee_name: str, args) -> list[IRExpr]:
            """Replace any actual that is a bare procedure name passed to a
            dummy-procedure parameter with a capturing lambda."""
            callee = by_name.get(callee_name)
            if callee is None or callee_name in shadowed:
                return list(args)
            out: list[IRExpr] = []
            for i, a in enumerate(args):
                if (
                    i < len(callee.parameters)
                    and callee.parameters[i].type.is_procedure
                    and isinstance(a, IRName)
                    and a.name in by_name
                ):
                    out.append(proc_lambda(a.name, callee.parameters[i].type.proc_arity))
                elif i < len(callee.parameters) and isinstance(a, IRName):
                    pun = _storage_pun(
                        a, caller_types.get(a.name), callee.parameters[i]
                    )
                    out.append(pun if pun is not None else a)
                else:
                    out.append(a)
            return out

        def rewrite_call(stmt: IRStatement) -> IRStatement:
            if not isinstance(stmt, IRCall):
                return stmt
            extra = state_args(stmt.callee)
            wrapped = wrap_proc_args(stmt.callee, stmt.args)
            if extra is None and wrapped == list(stmt.args):
                return stmt
            return IRCall(
                callee=stmt.callee,
                args=(extra or []) + wrapped,
                leading_comments=stmt.leading_comments,
                trailing_comments=stmt.trailing_comments,
            )

        def rewrite_fcall(expr: IRExpr) -> IRExpr:
            if not isinstance(expr, IRFunctionCall):
                return expr
            extra = state_args(expr.callee)
            wrapped = wrap_proc_args(expr.callee, expr.args)
            if extra is None and wrapped == list(expr.args):
                return expr
            return IRFunctionCall(
                callee=expr.callee, args=tuple(extra or []) + tuple(wrapped)
            )

        new_body = [
            # Wrap ``rewrite_fcall`` in ``map_expr`` so function calls
            # nested inside larger expressions (``x + f(y)``) are rewritten,
            # not just a statement's top-level expressions.
            map_statement(
                s,
                on_stmt=rewrite_call,
                on_expr=lambda e: map_expr(e, rewrite_fcall),
            )
            for s in caller.body
        ]
        if local_state_instances:
            instances = [
                IRLocal(
                    name=name,
                    type=IRType(cpp=struct_type, fortran=struct_type),
                    initializer=IRRaw("{}"),
                )
                for struct_type, name in local_state_instances.items()
            ]
            caller.locals = instances + caller.locals
        caller.body = new_body


def _shadowed_names(sub: IRSubprogram) -> set[str]:
    """Names that, *within this routine*, denote data — locals, dummy
    arguments, and common/module/save/workspace-bound members.  A
    ``name(...)`` using one of these is array indexing or a substring, not
    a call, even when ``name`` collides with a global subprogram (a
    routine's local ``STPOOL`` array vs. a library ``STPOOL`` function).
    Such a name must not be treated as a callee for state plumbing."""
    return (
        {loc.name for loc in sub.locals}
        | {p.name for p in sub.parameters}
        | {b.name for b in sub.state_bindings}
    )


def _callee_names(
    body: list[IRStatement], exclude: frozenset[str] | set[str] = frozenset()
) -> list[str]:
    """Every routine called from ``body`` — both subroutine ``CALL``
    statements and function-call expressions — in encounter order.  Names
    in ``exclude`` (data shadowing a like-named subprogram) are skipped."""
    names: list[str] = []

    def on_stmt(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRCall) and stmt.callee not in exclude:
            names.append(stmt.callee)
        return stmt

    def note(expr: IRExpr) -> IRExpr:
        if isinstance(expr, IRFunctionCall) and expr.callee not in exclude:
            names.append(expr.callee)
        return expr

    for stmt in body:
        # ``map_statement``'s ``on_expr`` only sees each statement's
        # top-level expressions; wrap in ``map_expr`` to reach calls
        # nested inside larger expressions (``x + f(y)``).
        map_statement(stmt, on_stmt=on_stmt, on_expr=lambda e: map_expr(e, note))
    return names
