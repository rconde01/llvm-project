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
    IRDirectRead,
    IRDirectWrite,
    IRExpr,
    IRFilePosition,
    IRFunctionCall,
    IRInquire,
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
    IRUnformattedDirectRead,
    IRUnformattedDirectWrite,
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
    bound_fields: list[str | tuple[str, str] | tuple[str, str, str]],
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
        view: str | None = None
        if isinstance(entry, str):
            local_name = field_name = entry
        elif len(entry) == 2:
            local_name, field_name = entry
        else:
            local_name, field_name, view = entry
        if not any(
            b.name == local_name and b.param == param_name
            for b in sub.state_bindings
        ):
            sub.state_bindings.append(
                IRStateBinding(
                    name=local_name, param=param_name, field=field_name, view=view
                )
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


_EXTENT_LITERAL_RE = re.compile(r"^[\s\d\+\-\*\(\)]+$")


def _eval_constant_extent(text: str) -> int:
    """Parse a small constant-arithmetic extent expression to an int.

    Restricted to digits and ``+ - * ( )`` so a malformed extent can't
    execute arbitrary code through eval.  Raises ``ValueError`` for
    anything else."""
    text = text.strip()
    if not _EXTENT_LITERAL_RE.match(text):
        raise ValueError(f"non-constant extent: {text!r}")
    return int(eval(text, {"__builtins__": {}}, {}))


def _common_elem_size(t: IRType) -> int:
    """Bytes-per-element for a Fortran scalar type, for COMMON layout.

    The byte size is the *Fortran storage size* (so the byte-offset
    layout aligns across declarations), not the C++ object size.
    Notably, Fortran's default LOGICAL is 4 bytes -- a routine that
    declares ``LOGICAL RZINO`` in a common block takes the same storage
    slot as another's ``REAL RZINO`` even though the C++ types are
    ``bool`` (1 byte) vs ``float`` (4 bytes).

    Defaults to 4 (Fortran default REAL/INTEGER) when the type isn't a
    known fixed-width spelling.  Character types are sized by the C++
    spelling's character count when feasible; otherwise 1."""
    if t.is_logical:
        return 4
    cpp = (t.element_type_cpp or t.cpp).strip()
    sizes = {
        "bool": 4,  # Fortran LOGICAL kind 4 -- see comment above
        "char": 1, "signed char": 1, "unsigned char": 1,
        "std::int8_t": 1, "std::uint8_t": 1,
        "std::int16_t": 2, "std::uint16_t": 2,
        "std::int32_t": 4, "std::uint32_t": 4,
        "std::int64_t": 8, "std::uint64_t": 8,
        "float": 4, "double": 8, "long double": 16,
    }
    if cpp in sizes:
        return sizes[cpp]
    if cpp.startswith("fortran::FortranString<"):
        try:
            n = int(cpp[cpp.index("<") + 1 : cpp.rindex(">")])
            return n
        except ValueError:
            return 1
    return 4


def _common_member_byte_size(t: IRType | None) -> int | None:
    """Total byte size of a COMMON member's type: ``elem_size *
    product(extents)``.  Returns ``None`` for unknown shape (deferred
    or assumed).  Extents are usually plain integers, occasionally
    simple arithmetic (``"(23) - (0) + 1"`` for ``A(0:23)``)."""
    if t is None:
        return None
    elem = _common_elem_size(t)
    if not t.is_array:
        return elem
    if not t.array_extent_exprs or not t.array_static:
        return None
    try:
        n = 1
        for e in t.array_extent_exprs:
            n *= _eval_constant_extent(e)
        return elem * n
    except (ValueError, TypeError, SyntaxError):
        return None


def _build_common_structs(tu: IRTranslationUnit) -> None:
    """A common block is shared storage declared (re-)independently in
    each routine that uses it.  Fortran COMMON members are positional in
    *byte storage*: one routine's ``common /c/ pt`` and another's
    ``common /c/ pt1, pt2, pt3`` over the same block describe the same
    bytes partitioned differently.  We track byte offsets rather than
    declaration position so a finer-grained user (e.g. IRI NRLMSISE-00's
    BLOCK DATA, which declares ``PT1(50)/PT2(50)/PT3(50)`` overlaying
    canonical ``PT(150)``) can bind each variable to a *sub-view* of the
    canonical field that contains it.

    The canonical layout is the one covering the largest total byte
    span, ties broken by COARSER (fewest members) — so the canonical
    fits every other routine's storage, and a finer-grained user's
    variable doesn't span multiple canonical fields."""
    # Per (sub index, block_name): list of (name, byte-offset, byte-size,
    # IRType) -- the layout this routine declared for this block.
    per_use_layout: dict[
        tuple[int, str],
        list[tuple[str, int, int | None, IRType | None]],
    ] = {}
    for idx, sub in enumerate(tu.subprograms):
        local_types = {loc.name: loc.type for loc in sub.locals}
        for use in sub.common_uses:
            layout: list[tuple[str, int, int | None, IRType | None]] = []
            off = 0
            for m in use.member_names:
                lt = local_types.get(m)
                sz = _common_member_byte_size(lt)
                layout.append((m, off, sz, lt))
                if sz is None:
                    break
                off += sz
            per_use_layout[(idx, use.block_name)] = layout

    if not per_use_layout:
        return

    # Pick a canonical layout per block: largest total bytes (so every
    # other layout's storage fits), ties broken by COARSER (fewest
    # members).  A coarser canonical lets a finer-grained user map each
    # variable to a sub-view of the canonical field that contains it.
    canonicals: dict[str, list[tuple[str, int, int | None, IRType | None]]] = {}
    for (idx, block_name), layout in per_use_layout.items():
        current = canonicals.get(block_name)
        layout_sized = layout and all(sz is not None for _, _, sz, _ in layout)
        current_sized = (
            current is not None and current
            and all(sz is not None for _, _, sz, _ in current)
        )
        layout_total = (
            sum(sz for _, _, sz, _ in layout if sz is not None)
            if layout_sized else 0
        )
        current_total = (
            sum(sz for _, _, sz, _ in current if sz is not None)
            if current_sized else 0
        )
        if current is None:
            canonicals[block_name] = layout
        elif layout_sized and not current_sized:
            canonicals[block_name] = layout
        elif layout_sized and current_sized:
            # Tie-breaker: prefer a layout that contains CHARACTER members
            # over one that has only implicit-typed numeric members at the
            # same offsets.  This avoids the NRLMSISE-00 DATIM7 case where
            # BLOCK DATA declares CHARACTER*4 ISDATE/ISTIME/NAME at the
            # same offsets the driver's other routines (implicitly) declare
            # them as INTEGER -- picking the INTEGER layout would force a
            # string_view->int assignment that doesn't compile.
            layout_has_char = any(
                t is not None and t.is_character for _, _, _, t in layout
            )
            current_has_char = any(
                t is not None and t.is_character for _, _, _, t in current
            )
            if layout_total > current_total:
                canonicals[block_name] = layout
            elif layout_total == current_total:
                if layout_has_char and not current_has_char:
                    canonicals[block_name] = layout
                elif (
                    layout_has_char == current_has_char
                    and len(layout) < len(current)
                ):
                    canonicals[block_name] = layout

    struct_for_block: dict[str, IRStateStruct] = {}
    # Per block: canonical layout list of (name, off, sz, type).
    canon_layout_for_block: dict[
        str, list[tuple[str, int, int | None, IRType | None]]
    ] = {}
    for block_name, slots in canonicals.items():
        used: set[str] = set()
        fields: list[IRLocal] = []
        canon_layout: list[tuple[str, int, int | None, IRType | None]] = []
        for pos, (canon, off, sz, t) in enumerate(slots):
            # The same source name can land at two distinct offsets when
            # other declarations diverge -- disambiguate by position.
            name = canon if canon not in used else f"{canon}__p{pos}"
            used.add(name)
            canon_layout.append((name, off, sz, t))
            fields.append(
                IRLocal(
                    name=name,
                    type=t or IRType(cpp="/* TODO: type */ double", fortran="?"),
                )
            )
        struct_for_block[block_name] = IRStateStruct(
            cpp_type=_common_struct_name(block_name), fields=fields
        )
        canon_layout_for_block[block_name] = canon_layout
    for struct in struct_for_block.values():
        tu.common_structs.append(struct)

    for idx, sub in enumerate(tu.subprograms):
        param_names = {p.name for p in sub.parameters}
        for block_name in {b for (i, b) in per_use_layout if i == idx}:
            struct = struct_for_block[block_name]
            canon_layout = canon_layout_for_block[block_name]
            param = _common_param_name(block_name)
            members = per_use_layout[(idx, block_name)]
            member_set = {m for m, _, _, _ in members}
            # This routine's own common members are also declared as
            # locals in Fortran; drop them.
            sub.locals = [
                loc for loc in sub.locals if loc.name not in member_set
            ]
            bound: list[str | tuple[str, str] | tuple[str, str, str]] = []
            for local_name, off, sz, lt in members:
                if local_name in param_names:
                    continue
                match = _canon_field_for_offset(
                    canon_layout, off, sz, lt, param
                )
                if match is None:
                    continue  # unmappable (unknown size or spans fields)
                field, view = match
                if view is not None:
                    bound.append((local_name, field, view))
                else:
                    bound.append(
                        local_name if local_name == field
                        else (local_name, field)
                    )
            _attach_state(
                sub,
                struct_type=struct.cpp_type,
                param_name=_common_param_name(block_name),
                owned_by="__common_" + block_name,
                bound_fields=bound,
            )


def _canon_field_for_offset(
    canon_layout: list[tuple[str, int, int | None, IRType | None]],
    off: int,
    sz: int | None,
    routine_type: IRType | None,
    param: str,
) -> tuple[str, str | None] | None:
    """Map a routine's COMMON member at byte ``off`` (size ``sz``, type
    ``routine_type``) to a canonical field name + optional view expression.

    Returns ``(field_name, None)`` for an exact-match rename binding, or
    ``(field_name, view_expr)`` for a sub-view (e.g. routine's PT1(50)
    over canonical PT(150) at offset 0).  Returns ``None`` when no
    suitable canonical exists -- the size is unknown, or the routine
    variable spans multiple canonical fields."""
    for cname, coff, csz, ctype in canon_layout:
        if csz is None:
            continue
        if coff == off and csz == sz:
            # Same offset + size: rename, or same-element shape reshape.
            view = _common_reshape_view(routine_type, ctype, param, cname)
            return (cname, view)
        if coff <= off < coff + csz:
            if sz is None:
                return None
            if off + sz > coff + csz:
                # Routine variable spans multiple canonical fields -- the
                # NRLMSISE-00 pattern where the driver declares
                # ``COMMON/GTS3C/DL(16)`` (one 16-float array) but the
                # subroutine declares ``GTS3C`` as 17 separate scalars
                # (TLB, S, DB04, ...).  Express it as an ArrayRef over the
                # consecutive canonical fields' storage, taking advantage
                # of the C++ struct's contiguous same-typed scalar layout.
                view = _common_spanview(
                    routine_type, canon_layout, off, sz, param
                )
                if view is None:
                    return None
                return (cname, view)
            view = _common_subview(
                routine_type, ctype, param, cname, off - coff, sz
            )
            if view is None:
                return None
            return (cname, view)
    return None


def _common_spanview(
    routine_type: IRType | None,
    canon_layout: list[tuple[str, int, int | None, IRType | None]],
    off: int,
    sz: int,
    param: str,
) -> str | None:
    """A routine's array variable that spans multiple consecutive canonical
    scalar fields -- emit an ArrayRef view starting at the first canonical
    field that falls inside the routine variable's byte range.

    Requires the canonical fields the variable covers to all share the
    same scalar element type and to be laid out consecutively in the
    struct (same type implies same alignment with no padding between
    members of a POD struct in standard layout)."""
    if routine_type is None or not routine_type.is_array:
        return None
    elem_cpp = routine_type.element_type_cpp
    if not elem_cpp:
        return None
    elem = _common_elem_size(routine_type)
    if elem == 0 or sz % elem != 0:
        return None
    # Find canonical fields covering [off, off+sz).  Require every covered
    # field to have the same element type as the routine's; require the
    # span to start *at* a canonical field's offset (so we anchor the view).
    covered: list[tuple[str, int, int]] = []
    for cname, coff, csz, ctype in canon_layout:
        if csz is None:
            continue
        if coff >= off + sz:
            break
        if coff + csz <= off:
            continue
        # Overlaps -- require full containment within [off, off+sz).
        if coff < off or coff + csz > off + sz:
            return None
        if ctype is None or (ctype.element_type_cpp or ctype.cpp) != elem_cpp:
            return None
        covered.append((cname, coff, csz))
    if not covered or covered[0][1] != off:
        return None
    if covered[-1][1] + covered[-1][2] != off + sz:
        return None
    extents = routine_type.array_extent_exprs
    if not extents:
        return None
    ext_list = ", ".join(extents)
    anchor = covered[0][0]
    return (
        f"fortran::ArrayRef<{elem_cpp}, {routine_type.array_rank}>("
        f"&{param}.{anchor}, {{{ext_list}}})"
    )


def _common_subview(
    routine_type: IRType | None,
    canon_type: IRType | None,
    param: str,
    field: str,
    byte_off_within: int,
    byte_size: int,
) -> str | None:
    """Build an ArrayRef sub-view (or scalar reference) for a routine
    variable that lies *inside* a canonical COMMON field at byte offset
    ``byte_off_within`` covering ``byte_size`` bytes (the IRI
    NRLMSISE-00 ``PT1(50)`` overlay on canonical ``PT(150)`` at offset
    0)."""
    if routine_type is None or canon_type is None:
        return None
    routine_elem_cpp = routine_type.element_type_cpp or routine_type.cpp
    canon_elem_cpp = canon_type.element_type_cpp or canon_type.cpp
    type_pun = routine_elem_cpp != canon_elem_cpp
    elem = _common_elem_size(routine_type)
    if elem == 0:
        return None
    if not type_pun:
        # Same element type: require element-aligned offset / size so the
        # ArrayRef sub-view starts at an element boundary.
        if byte_off_within % elem != 0 or byte_size % elem != 0:
            return None
        elem_off = byte_off_within // elem
        elem_count = byte_size // elem
        if not routine_type.is_array:
            # A scalar routine variable inside an array canonical: emit a
            # reference to the appropriate element (Fortran 1-based).
            return (
                f"{param}.{field}"
                f"(static_cast<fortran::index_t>({elem_off + 1}))"
            )
        if not routine_type.array_extent_exprs:
            return None
        rank = routine_type.array_rank
        ext_list = ", ".join(routine_type.array_extent_exprs)
        return (
            f"fortran::ArrayRef<{routine_type.element_type_cpp}, {rank}>("
            f"{param}.{field}.data() + {elem_off}, {{{ext_list}}})"
        )
    # Type pun: the routine's view interprets the canonical field's bytes
    # as a different element type (the F77 ``CHARACTER ISDATE(3)`` view
    # over an implicit-typed ``INTEGER ISDATE(3)`` storage in MSIS-86's
    # DATIME common).  Emit a ``reinterpret_cast`` view -- safe as long
    # as the source and destination element types are trivially copyable
    # PODs, which the runtime's scalar types and ``FortranString<N>`` are.
    # The byte offset may be unaligned to the routine's element when the
    # routine view straddles a canonical field at a different element
    # size (PRMSG5's ``CHARACTER*4 ISTIME(2)`` at byte 3, inside the
    # canonical ``INTEGER ISDATE(3)``).  ``FortranString<N>`` and ``char``
    # have alignof 1 so a byte-offset reinterpret is well-defined.
    if not routine_type.is_array:
        return (
            f"*reinterpret_cast<{routine_elem_cpp}*>("
            f"reinterpret_cast<unsigned char*>({param}.{field}.data())"
            f" + {byte_off_within})"
        )
    if not routine_type.array_extent_exprs:
        return None
    rank = routine_type.array_rank
    ext_list = ", ".join(routine_type.array_extent_exprs)
    return (
        f"fortran::ArrayRef<{routine_elem_cpp}, {rank}>("
        f"reinterpret_cast<{routine_elem_cpp}*>("
        f"reinterpret_cast<unsigned char*>({param}.{field}.data())"
        f" + {byte_off_within}), {{{ext_list}}})"
    )


def _common_reshape_view(
    member_type: IRType | None,
    canon_type: IRType | None,
    param: str,
    field: str,
) -> str | None:
    """Build a reshaped ``ArrayRef`` view of a shared COMMON field when this
    routine declares the member with a *different array shape* or *element
    type* than the block's canonical field (storage association).

    Same element type, different shape (IRI's ``/BLWRK/`` declares
    ``WA(216)`` in one routine and ``WA(36,6)`` in another over the same
    storage): emit a reshaped ArrayRef.

    Different element type at the same offset and total size (MSIS-86's
    PRMSG5 declares ``CHARACTER*4 ISDATE(3)`` over the canonical
    ``INTEGER ISDATE(3)``): emit a reinterpret_cast ArrayRef so PRMSG5
    writes characters into the canonical integer storage.
    """
    if member_type is None or canon_type is None:
        return None
    if not (member_type.is_array and canon_type.is_array):
        # Scalar pun on a scalar canonical field: emit a reinterpreted
        # reference so the routine can assign through it.
        if (
            not member_type.is_array
            and not canon_type.is_array
            and member_type.cpp != canon_type.cpp
        ):
            return (
                f"*reinterpret_cast<{member_type.cpp}*>("
                f"&{param}.{field})"
            )
        return None
    extents = member_type.array_extent_exprs
    if not extents or len(extents) != member_type.array_rank:
        return None
    same_element = (
        member_type.element_type_cpp == canon_type.element_type_cpp
    )
    same_shape = (
        member_type.array_rank == canon_type.array_rank
        and member_type.array_extent_exprs == canon_type.array_extent_exprs
    )
    if same_element and same_shape:
        return None
    elem = member_type.element_type_cpp
    ext_list = ", ".join(extents)
    if same_element:
        # Reshape only.
        return (
            f"fortran::ArrayRef<{elem}, {member_type.array_rank}>("
            f"{param}.{field}.data(), {{{ext_list}}})"
        )
    # Type pun: same offset (0) and total size; differ only in element
    # type.  The canonical field's storage is contiguous bytes; reinterpret
    # them as the routine's element type.
    return (
        f"fortran::ArrayRef<{elem}, {member_type.array_rank}>("
        f"reinterpret_cast<{elem}*>({param}.{field}.data()), "
        f"{{{ext_list}}})"
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
        # Statements that emit ``_units.<op>(...)`` directly: INQUIRE,
        # BACKSPACE/REWIND, and direct-access / unformatted record I/O.
        # (fndlun/errfnm reach the units table only through INQUIRE.)
        if isinstance(
            stmt,
            (
                IRInquire,
                IRFilePosition,
                IRDirectRead,
                IRDirectWrite,
                IRUnformattedDirectRead,
                IRUnformattedDirectWrite,
            ),
        ):
            found[0] = True
        elif isinstance(stmt, IRCall) and stmt.callee.startswith(_UNITS_PARAM + "."):
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
            whatever the callee invokes it with (``auto&&...``) and passes
            those after the captured state arguments.  A generic lambda is a
            concrete object with its own type, so even a higher-order routine
            (itself a template) can be passed this way — deduction latches
            onto the closure, not the un-instantiable template name.

            The forwarded arguments are passed as the *named* parameters
            ``_a...`` (lvalues), not ``std::forward``-ed, because Fortran
            argument association is by reference: a modifiable scalar dummy
            (``float&``) must bind even when the caller invoked the callback
            with an expression (``func(x+hh)`` in Numerical Recipes' DFRIDR).
            Fortran materializes a temporary for such an expression actual
            and discards any write to it; binding the named ``_a`` (an lvalue
            referring to that temporary) reproduces exactly that, while an
            lvalue actual still has writes propagate back."""
            sargs = state_args(actual_name) or []
            state = "".join(
                f"{e.name}, " for e in sargs if isinstance(e, IRName)
            )
            actual = by_name.get(actual_name)
            ret = "return " if (actual is not None and actual.kind == "function") else ""
            return IRRaw(
                f"[&](auto&&... _a) {{ {ret}{actual_name}({state}_a...); }}"
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
