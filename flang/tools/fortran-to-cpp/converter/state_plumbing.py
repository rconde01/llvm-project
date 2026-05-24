"""Post-lowering pass that plumbs persistent state through call chains.

For now this only handles SAVE locals.  Common blocks and module
variables will land in subsequent passes following the same pattern.

The pass runs after lowering (which marks ``is_save`` on IRLocal) and
before emission.  It mutates the translation unit in place to:

  1. Move every subprogram's SAVE locals onto a generated state struct
     (the subprogram "owns" that struct).
  2. Rewrite the subprogram's body so references to the saved
     variables become ``<param>.<name>``.
  3. Add a state parameter to the subprogram's signature.
  4. Walk the call graph and forward each save struct up to wherever
     it needs to be allocated.  The first non-state-parameterised
     caller in each chain owns the *instance* (allocated as a local
     IRLocal) and passes it to every callee in its dynamic extent.
  5. Rewrite each ``IRCall`` to prepend the state arguments expected
     by the callee, in the order declared in callee.state_params.
"""

from __future__ import annotations

from .ir import (
    IRCall,
    IRDo,
    IRExpr,
    IRIf,
    IRLocal,
    IRName,
    IRRaw,
    IRSelectCase,
    IRStateParam,
    IRStateStruct,
    IRStatement,
    IRSubprogram,
    IRTranslationUnit,
    IRType,
    IRWhile,
)
from .transform import map_statement, rename_var


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plumb_state(tu: IRTranslationUnit) -> None:
    """Run all state-plumbing transformations on ``tu``.

    Mutates the translation unit in place.
    """
    _build_common_structs(tu)
    _build_save_structs(tu)
    _propagate_state_parameters(tu)
    _rewrite_call_sites(tu)


# ---------------------------------------------------------------------------
# Common blocks
# ---------------------------------------------------------------------------


def _build_common_structs(tu: IRTranslationUnit) -> None:
    """Synthesize one shared struct per common-block name and rewrite
    every using subprogram to reference it.

    Member types are resolved from each subprogram's own locals (the
    block members are also declared as regular locals in Fortran), so
    we pull those declarations onto the struct and drop them from the
    local list.
    """
    # Gather the union of member names per block (in first-seen order)
    # and resolve a type for each from whichever subprogram declares it.
    block_members: dict[str, list[str]] = {}
    block_member_types: dict[str, dict[str, IRType]] = {}
    for sub in tu.subprograms:
        local_types = {loc.name: loc.type for loc in sub.locals}
        for use in sub.common_uses:
            members = block_members.setdefault(use.block_name, [])
            types = block_member_types.setdefault(use.block_name, {})
            for m in use.member_names:
                if m not in members:
                    members.append(m)
                if m not in types and m in local_types:
                    types[m] = local_types[m]

    if not block_members:
        return

    # Build a struct + (param-name, struct-type) for each block.
    struct_for_block: dict[str, IRStateStruct] = {}
    for block_name, members in block_members.items():
        struct_type = _common_struct_name(block_name)
        types = block_member_types.get(block_name, {})
        fields = [
            IRLocal(
                name=m,
                type=types.get(m, IRType(cpp="/* TODO: type */ double",
                                         fortran="?")),
            )
            for m in members
        ]
        struct_for_block[block_name] = IRStateStruct(
            cpp_type=struct_type, fields=fields
        )

    for struct in struct_for_block.values():
        tu.common_structs.append(struct)

    # Rewrite each using subprogram.
    for sub in tu.subprograms:
        used_blocks = {u.block_name for u in sub.common_uses}
        for block_name in used_blocks:
            struct = struct_for_block[block_name]
            param_name = _common_param_name(block_name)
            # A non-main routine receives the block as a state
            # parameter; the main program *owns* the instance as a
            # local (it can't take parameters).  Either way, body
            # references to the members become ``<name>.<member>``.
            if sub.kind == "main":
                if not any(
                    loc.name == param_name for loc in sub.locals
                ):
                    sub.locals.insert(
                        0,
                        IRLocal(
                            name=param_name,
                            type=IRType(cpp=struct.cpp_type,
                                        fortran=struct.cpp_type),
                            initializer=IRRaw("{}"),
                        ),
                    )
            else:
                if not any(
                    sp.struct_type == struct.cpp_type for sp in sub.state_params
                ):
                    sub.state_params.append(
                        IRStateParam(
                            name=param_name,
                            struct_type=struct.cpp_type,
                            owned_by="__common_" + block_name,
                        )
                    )
            # Drop the block members from this sub's locals and rewrite
            # references to ``<param>.<member>``.
            member_names = {f.name for f in struct.fields}
            sub.locals = [
                loc for loc in sub.locals if loc.name not in member_names
            ]
            for m in member_names:
                sub.body = [
                    _rewrite_to_field_access(s, m, param_name)
                    for s in sub.body
                ]


def _common_struct_name(block_name: str) -> str:
    if not block_name:
        return "BlankCommon"
    return _camelcase(block_name) + "Common"


def _common_param_name(block_name: str) -> str:
    if not block_name:
        return "blank_common"
    return block_name + "_common"


# ---------------------------------------------------------------------------
# Step 1: extract SAVE locals into per-subprogram state structs
# ---------------------------------------------------------------------------


def _build_save_structs(tu: IRTranslationUnit) -> None:
    for sub in tu.subprograms:
        save_locals = [loc for loc in sub.locals if loc.is_save]
        if not save_locals:
            continue
        struct_type = _camelcase(sub.display_name) + "Save"
        param_name = sub.name + "_save"
        sub.save_struct = IRStateStruct(
            cpp_type=struct_type,
            fields=save_locals,
        )
        # Remove saved locals from the regular local list.
        sub.locals = [loc for loc in sub.locals if not loc.is_save]
        # The own-save struct becomes the first state parameter.
        sub.state_params.insert(
            0,
            IRStateParam(
                name=param_name,
                struct_type=struct_type,
                owned_by=sub.name,
            ),
        )
        # Rewrite the body: bare references to a saved name become
        # ``<param_name>.<name>``.
        for loc in save_locals:
            sub.body = [
                _rewrite_to_field_access(s, loc.name, param_name)
                for s in sub.body
            ]


def _camelcase(name: str) -> str:
    """Turn a Fortran identifier into a CamelCase struct name."""
    parts = name.replace("-", "_").split("_")
    return "".join(p.capitalize() if p else "" for p in parts)


# ---------------------------------------------------------------------------
# Step 2: forward state parameters up call chains
# ---------------------------------------------------------------------------


def _propagate_state_parameters(tu: IRTranslationUnit) -> None:
    """For each call site, ensure the caller receives (or owns) every
    state struct the callee expects.

    We iterate until the per-subprogram state-parameter set stops
    growing.  A subprogram that doesn't *own* a struct but *calls*
    something that needs one must forward the parameter from its own
    signature.  Mains never gain parameters — they instantiate the
    struct as a local instead (handled in step 3).
    """
    by_name = {s.name: s for s in tu.subprograms}
    changed = True
    while changed:
        changed = False
        for caller in tu.subprograms:
            if caller.kind == "main":
                # Main can't grow state parameters — it'll own the
                # instances locally at step 3.
                continue
            for stmt in _all_calls(caller.body):
                callee = by_name.get(stmt.callee)
                if callee is None:
                    continue
                for sp in callee.state_params:
                    if any(
                        existing.struct_type == sp.struct_type
                        for existing in caller.state_params
                    ):
                        continue
                    # Caller doesn't have this state yet.  Forward it
                    # under the callee's parameter name (which is
                    # globally unique: ``<owner>_save`` / ``<block>_common``).
                    caller.state_params.append(
                        IRStateParam(
                            name=sp.name,
                            struct_type=sp.struct_type,
                            owned_by=sp.owned_by,
                        )
                    )
                    changed = True


# ---------------------------------------------------------------------------
# Step 3: at every call site, prepend the state arguments
# ---------------------------------------------------------------------------


def _rewrite_call_sites(tu: IRTranslationUnit) -> None:
    by_name = {s.name: s for s in tu.subprograms}
    all_struct_types = {
        sp.struct_type for s in tu.subprograms for sp in s.state_params
    } | {st.cpp_type for st in tu.common_structs}
    for caller in tu.subprograms:
        # For each callee state param, decide what to pass:
        #   * If caller has a matching state_param of its own, forward
        #     it by name.
        #   * If the caller already owns a local instance of that
        #     struct (e.g. main owning a common block), reuse it.
        #   * Otherwise (main calling a SAVE routine), allocate a fresh
        #     local IRLocal of that struct type and pass it.
        caller_state_names = {sp.struct_type: sp.name for sp in caller.state_params}
        # Seed with any owned local instances (common blocks in main).
        for loc in caller.locals:
            if loc.type.cpp in all_struct_types:
                caller_state_names.setdefault(loc.type.cpp, loc.name)
        local_state_instances: dict[str, str] = {}  # struct_type -> local name
        def rewrite_call(stmt: IRStatement) -> IRStatement:
            if not isinstance(stmt, IRCall):
                return stmt
            callee = by_name.get(stmt.callee)
            if callee is None or not callee.state_params:
                return stmt
            extra_args: list[IRExpr] = []
            for sp in callee.state_params:
                if sp.struct_type in caller_state_names:
                    nm = caller_state_names[sp.struct_type]
                else:
                    nm = local_state_instances.setdefault(sp.struct_type, sp.name)
                extra_args.append(IRName(name=nm, fortran=nm))
            return IRCall(
                callee=stmt.callee,
                args=extra_args + list(stmt.args),
                leading_comments=stmt.leading_comments,
                trailing_comments=stmt.trailing_comments,
            )

        new_body = [
            map_statement(stmt, on_stmt=rewrite_call) for stmt in caller.body
        ]
        # Prepend any newly-created locals (state instances) so they
        # come before the first use.
        if local_state_instances:
            instance_locals = [
                IRLocal(
                    name=name,
                    # The state struct is value-initialized via {}
                    # so its scalar fields zero out, matching
                    # Fortran's typical -finit-zero behavior.
                    type=IRType(cpp=struct_type, fortran=struct_type),
                    initializer=IRRaw("{}"),
                )
                for struct_type, name in local_state_instances.items()
            ]
            caller.locals = instance_locals + caller.locals
        caller.body = new_body


def _all_calls(body: list[IRStatement]):
    """Yield every IRCall reachable inside ``body``, descending into all
    nested statement blocks."""
    calls: list[IRCall] = []

    def collect(stmt: IRStatement) -> IRStatement:
        if isinstance(stmt, IRCall):
            calls.append(stmt)
        return stmt

    for stmt in body:
        map_statement(stmt, on_stmt=collect)
    return calls


# ---------------------------------------------------------------------------
# Helpers — rewriting variable references to struct field accesses
# ---------------------------------------------------------------------------


def _rewrite_to_field_access(
    stmt: IRStatement, var_name: str, struct_param: str
) -> IRStatement:
    """Replace ``IRName(var_name)`` with ``IRName(<struct_param>.<var_name>)``
    everywhere inside ``stmt`` (including nested blocks)."""
    replacement = struct_param + "." + var_name
    return map_statement(
        stmt, on_expr=lambda e: rename_var(e, var_name, replacement)
    )
