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
    IRCaseClause,
    IRCycle,
    IRDerivedType,
    IRExit,
    IRMember,
    IRModule,
    IRSelectCase,
    IRWhile,
)
from .transform import map_statement, rename_var
from .types import camelcase, lower_type_spec


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def lower_program(
    root: Node, *, source_file: str | None = None
) -> IRTranslationUnit:
    """Lower an annotated, dependency-ordered parse tree to IR."""
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
    return tu


def _collect_units(
    node: Node, tu: IRTranslationUnit, *, parent_module: str | None
) -> None:
    """Recursively collect modules and subprograms, tracking the
    enclosing module so module procedures know their host."""
    for child in node.children:
        kind = child.kind
        if kind == "Module":
            _collect_module(child, tu)
        elif kind == "MainProgram":
            tu.subprograms.append(_lower_main_program(child))
        elif kind == "FunctionSubprogram":
            sub = _lower_function(child)
            sub.parent_module = parent_module
            tu.subprograms.append(sub)
        elif kind == "SubroutineSubprogram":
            sub = _lower_subroutine(child)
            sub.parent_module = parent_module
            tu.subprograms.append(sub)
        else:
            # Descend through containers (Program, ProgramUnit,
            # ModuleSubprogramPart, ModuleSubprogram, ...).
            _collect_units(child, tu, parent_module=parent_module)


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
        fortran_name=name.lower(),
    )
    # Module-level variable declarations live in the module's direct
    # SpecificationPart.
    for child in mod_node.children:
        if child.kind == "SpecificationPart":
            module.variables = _lower_specification(child)
    tu.modules.append(module)
    # Module procedures (in the CONTAINS section) are collected with
    # this module as their host.
    for child in mod_node.children:
        if child.kind == "ModuleSubprogramPart":
            _collect_units(child, tu, parent_module=module.fortran_name)


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
                # A component may carry its own ArraySpec (component array).
                arr = decl.first_child("ArraySpec")
                field_type = (
                    _make_array_type(comp_type, arr) if arr is not None
                    else comp_type
                )
                fields.append(IRLocal(name=name.fortran.lower(), type=field_type))
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
    sub = IRSubprogram(
        name=name.lower(),
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
        name=name.lower(),
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
    _separate_parameters(sub, dummy_arg_names)
    _lift_function_return(sub, prefix_return_type)
    return sub


def _lower_subroutine(node: Node) -> IRSubprogram:
    name = _extract_subprogram_name(node, "SubroutineStmt") or "anon_subroutine"
    sub = IRSubprogram(
        name=name.lower(),
        display_name=name,
        kind="subroutine",
        leading_comments=list(node.leading_comments),
        source=node.source,
    )
    dummy_arg_names = _extract_subroutine_dummy_args(node)
    _lower_specification_and_execution(node, sub)
    _separate_parameters(sub, dummy_arg_names)
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
                out.append(name.fortran)
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
                out.append(name.fortran)
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


def _separate_parameters(sub: IRSubprogram, arg_names: list[str]) -> None:
    """Pull every local matching a dummy arg name out into ``parameters``."""
    if not arg_names:
        return
    wanted = {a.lower(): i for i, a in enumerate(arg_names)}
    by_idx: list[tuple[int, IRParameter, IRLocal]] = []
    remaining: list[IRLocal] = []
    for loc in sub.locals:
        if loc.name in wanted:
            param = IRParameter(
                name=loc.name,
                type=loc.type,
                intent=loc.intent or "inout",
            )
            by_idx.append((wanted[loc.name], param, loc))
        else:
            remaining.append(loc)
    by_idx.sort(key=lambda t: t[0])
    sub.parameters = [p for _, p, _ in by_idx]
    sub.locals = remaining


def _lift_function_return(
    sub: IRSubprogram, prefix_type: IRType | None
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
        # No way to determine the return type; leave it as auto and let
        # the user fix it.
        return_type = IRType(cpp="auto", fortran="<inferred>")
    sub.return_type = return_type

    result_name = sub.name + "_result"
    sub.locals.insert(
        0,
        IRLocal(name=result_name, type=return_type),
    )
    sub.body = [
        map_statement(s, on_expr=lambda e: rename_var(e, sub.name, result_name))
        for s in sub.body
    ]


def _extract_subprogram_name(node: Node, header_kind: str) -> str | None:
    """Pull the declared subprogram name out of its header statement."""
    for stmt in node.children:
        if stmt.kind != "Statement":
            continue
        for inner in stmt.walk():
            if inner.kind == header_kind:
                for n in inner.walk():
                    if n.kind == "Name" and n.fortran:
                        return n.fortran
                return None
    return None


def _lower_specification_and_execution(node: Node, sub: IRSubprogram) -> None:
    """Walk the SpecificationPart (declarations) and ExecutionPart (body)."""
    for child in node.children:
        if child.kind == "SpecificationPart":
            sub.locals.extend(_lower_specification(child))
            sub.common_uses.extend(_lower_common_statements(child))
            sub.used_modules.extend(_lower_use_statements(child))
        elif child.kind == "ExecutionPart":
            sub.body.extend(_lower_execution(child))


def _lower_use_statements(spec_part: Node) -> list[str]:
    """Collect the module names imported by ``use`` statements."""
    out: list[str] = []
    for use in spec_part.find_all("UseStmt"):
        name = use.find_first("Name")
        if name is not None and name.fortran:
            out.append(name.fortran.lower())
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
                block_name = leading_name.fortran.lower()
            for obj in block.children_of_kind("CommonBlockObject"):
                name = obj.find_first("Name")
                if name is not None and name.fortran:
                    members.append(name.fortran.lower())
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
    return out


def _lower_type_declaration(decl: Node) -> list[IRLocal]:
    """One ``TypeDeclarationStmt`` may declare several names sharing a type."""
    type_node = decl.first_child("DeclarationTypeSpec")
    if type_node is None:
        return []
    ir_type = lower_type_spec(type_node)

    is_parameter = False
    is_save = False
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
        initializer = None
        init = entity.find_first("Initialization")
        if init is not None:
            expr = init.find_first("Expr") or init.find_first("ConstantExpr")
            if expr is not None:
                initializer = _lower_expression(expr)
        per_entity_array = entity.first_child("ArraySpec")
        array_spec = per_entity_array if per_entity_array is not None else shared_array_spec
        loc_type = (
            _make_array_type(ir_type, array_spec) if array_spec else ir_type
        )
        out.append(
            IRLocal(
                name=name_node.fortran.lower(),
                type=loc_type,
                initializer=initializer,
                is_parameter=is_parameter,
                is_save=is_save,
                intent=intent,
            )
        )
    return out


def _make_array_type(element_type: IRType, array_spec: Node) -> IRType:
    """Wrap ``element_type`` in ``fortran::Array<T, Rank>`` with
    extent / lower-bound expressions extracted from ``array_spec``."""
    extents: list[str] = []
    lowers: list[str] = []
    has_explicit_lower = False
    for shape in array_spec.children:
        if shape.kind == "ExplicitShapeSpec":
            lo, hi = _lower_explicit_shape(shape)
            if lo is not None:
                lowers.append(lo)
                has_explicit_lower = True
            else:
                lowers.append("1")
            extents.append(hi)
        elif shape.kind in (
            "AssumedShapeSpec",
            "DeferredShapeSpec",
            "AssumedSizeSpec",
        ):
            # Unsupported for now; the user will get a TODO when the
            # emitted code fails to compile.
            extents.append(f"/* TODO: {shape.kind} */ 0")
            lowers.append("1")
    rank = len(extents)
    return IRType(
        cpp=f"fortran::Array<{element_type.cpp}, {rank}>",
        fortran=f"{element_type.fortran}, dimension({len(extents)})",
        is_array=True,
        array_rank=rank,
        array_extent_exprs=tuple(extents),
        array_lower_bound_exprs=tuple(lowers) if has_explicit_lower else (),
        element_type_cpp=element_type.cpp,
        is_integer=element_type.is_integer,
        is_real=element_type.is_real,
        is_logical=element_type.is_logical,
        is_character=element_type.is_character,
    )


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
        stmt = _lower_construct(construct)
        if stmt is not None:
            out.append(stmt)
    return out


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
    return _unsupported(target, kind=target.kind)


def _lower_action_statement(stmt: Node) -> IRStatement | None:
    """A ``Statement`` wrapper around an ``ActionStmt``."""
    leading = list(stmt.leading_comments)
    trailing = list(stmt.trailing_comments)
    action = stmt.find_first("ActionStmt")
    if action is None:
        return _unsupported(stmt, kind="Statement")
    inner = next(iter(action.children), None)
    if inner is None:
        return _unsupported(action, kind="ActionStmt")
    if inner.kind == "AssignmentStmt":
        a = _lower_assignment(inner)
        a.leading_comments = leading
        a.trailing_comments = trailing
        return a
    if inner.kind == "PrintStmt":
        p = _lower_print(inner)
        p.leading_comments = leading
        p.trailing_comments = trailing
        return p
    if inner.kind == "WriteStmt":
        p = _lower_write(inner)
        p.leading_comments = leading
        p.trailing_comments = trailing
        return p
    if inner.kind == "CallStmt":
        c = _lower_call(inner)
        c.leading_comments = leading
        c.trailing_comments = trailing
        return c
    if inner.kind == "ReturnStmt":
        return IRReturn(leading_comments=leading, trailing_comments=trailing)
    if inner.kind == "CycleStmt":
        return IRCycle(leading_comments=leading, trailing_comments=trailing)
    if inner.kind == "ExitStmt":
        return IRExit(leading_comments=leading, trailing_comments=trailing)
    if inner.kind == "IfStmt":
        return _lower_if_stmt(inner, leading, trailing)
    return _unsupported(inner, kind=inner.kind, leading=leading)


def _lower_if_stmt(
    node: Node, leading: list[Comment], trailing: list[Comment]
) -> IRStatement:
    """Lower a single-statement ``if (cond) action`` (no ``then``).

    Modeled as an IRIf with one branch holding the single action.
    """
    cond_expr = node.find_first("Expr")
    condition = _lower_expression(cond_expr) if cond_expr else IRRaw("true")
    # The action lives under an UnlabeledStatement -> ActionStmt.
    body: list[IRStatement] = []
    unlabeled = node.find_first("UnlabeledStatement")
    if unlabeled is not None:
        action = unlabeled.find_first("ActionStmt")
        if action is not None:
            inner = next(iter(action.children), None)
            if inner is not None:
                if inner.kind == "CycleStmt":
                    body = [IRCycle()]
                elif inner.kind == "ExitStmt":
                    body = [IRExit()]
                elif inner.kind == "ReturnStmt":
                    body = [IRReturn()]
                elif inner.kind == "AssignmentStmt":
                    body = [_lower_assignment(inner)]
                elif inner.kind == "PrintStmt":
                    body = [_lower_print(inner)]
                elif inner.kind == "CallStmt":
                    body = [_lower_call(inner)]
                else:
                    body = [_unsupported(inner, kind=inner.kind)]
    return IRIf(
        branches=[(condition, body)],
        else_body=None,
        leading_comments=leading,
        trailing_comments=trailing,
    )


def _lower_assignment(node: Node) -> IRAssignment:
    target = node.first_child("Variable") or node.first_child("Designator")
    value = node.first_child("Expr")
    return IRAssignment(
        target=_lower_expression(target) if target else IRRaw("/* ? */"),
        value=_lower_expression(value) if value else IRRaw("/* ? */"),
    )


def _lower_print(node: Node) -> IRPrint:
    items: list[IRExpr] = []
    for sub in node.children:
        if sub.kind == "OutputItem":
            expr = sub.find_first("Expr")
            if expr is not None:
                items.append(_lower_expression(expr))
    return IRPrint(items=items, format=_extract_format(node))


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
    stream = _stream_for_unit(node.first_child("IoUnit"))
    return IRPrint(items=items, stream=stream, format=_extract_format(node))


def _stream_for_unit(io_unit: Node | None) -> str:
    if io_unit is None:
        return "std::cout"
    if io_unit.first_child("Star") is not None:
        return "std::cout"
    # A literal unit number: map the conventional ones.
    for lit in io_unit.find_all("IntLiteralConstant"):
        if lit.fortran:
            num = lit.fortran.split("_")[0]
            if num == "0":
                return "std::cerr"
            if num in ("5", "6"):
                return "std::cout"
    return "std::cout"


def _extract_format(node: Node) -> str | None:
    """Return the format string for a Print/Write, or None for the
    list-directed (``*``) form."""
    fmt = node.first_child("Format")
    if fmt is None:
        return None
    if fmt.first_child("Star") is not None:
        return None
    # The format is usually a character literal; pull its body.
    for s in fmt.walk():
        if s.kind == "string" and s.fortran is not None:
            return s.fortran
    return None


def _lower_call(node: Node) -> IRCall:
    callee = ""
    for n in node.walk():
        if n.kind == "ProcedureDesignator":
            name = n.find_first("Name")
            if name and name.fortran:
                callee = name.fortran.lower()
                break
    args: list[IRExpr] = []
    for arg in node.find_all("ActualArgSpec"):
        expr = arg.find_first("Expr")
        if expr is not None:
            args.append(_lower_expression(expr))
    return IRCall(callee=callee, args=args)


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

    name = bounds.find_first("Name")
    var = name.fortran.lower() if name and name.fortran else "i"
    exprs = list(bounds.find_all("ScalarIntExpr")) or list(bounds.find_all("Expr"))
    # Expect [lower, upper] or [lower, upper, step].
    lo = _lower_expression(exprs[0]) if exprs else IRRaw("0")
    hi = _lower_expression(exprs[1]) if len(exprs) > 1 else IRRaw("0")
    step = _lower_expression(exprs[2]) if len(exprs) > 2 else None

    body = _lower_block(body_block) if body_block else []
    return IRDo(var=var, lower=lo, upper=hi, step=step, body=body)


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
    if target.kind in _BINARY_OP_MAP or target.kind in _UNARY_OP_MAP:
        return _lower_expr_operator(target)
    return _expr_raw(node)


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
        field_name_node.fortran.lower()
        if field_name_node is not None and field_name_node.fortran
        else "?"
    )
    return IRMember(base=base_expr, field=field)


def _lower_array_element(node: Node) -> IRExpr:
    """Translate ``a(i, j, k)`` to a call on the C++ Array object.

    fortran::Array overloads ``operator()`` with exactly the same
    arity / 1-based indexing as Fortran, so the translation is one
    IRFunctionCall whose callee is the array name and whose args are
    the lowered subscripts.
    """
    # First child is an inner DataRef that resolves to the array name.
    array_name = ""
    data_ref = node.first_child("DataRef")
    if data_ref is not None:
        name = data_ref.find_first("Name")
        if name is not None and name.fortran:
            array_name = name.fortran.lower()
    subscripts: list[IRExpr] = []
    for sub in node.children_of_kind("SectionSubscript"):
        # A SectionSubscript wraps either a single integer expression
        # (element access) or a SubscriptTriplet (slice).  For now we
        # only handle the element-access form.
        triplet = sub.find_first("SubscriptTriplet")
        if triplet is not None:
            return _expr_raw(node)  # TODO: array slicing
        expr = sub.find_first("Expr")
        if expr is not None:
            subscripts.append(_lower_expression(expr))
    return IRFunctionCall(callee=array_name, args=tuple(subscripts))


def _lower_name(node: Node) -> IRName:
    fortran = node.fortran or (node.source.text if node.source else "?")
    return IRName(name=fortran.lower(), fortran=fortran)


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
    "sqrt": "std::sqrt", "abs": "std::abs", "exp": "std::exp",
    "log": "std::log", "log10": "std::log10",
    "sin": "std::sin", "cos": "std::cos", "tan": "std::tan",
    "asin": "std::asin", "acos": "std::acos", "atan": "std::atan",
    "atan2": "std::atan2", "sinh": "std::sinh", "cosh": "std::cosh",
    "tanh": "std::tanh", "floor": "std::floor", "ceiling": "std::ceil",
    "min": "std::min", "max": "std::max",
    "mod": "std::fmod",  # Fortran MOD follows truncation, like fmod
    "modulo": "std::fmod",
    "sign": "std::copysign",
}


def _lower_function_reference(node: Node) -> IRFunctionCall:
    callee = ""
    for n in node.walk():
        if n.kind == "ProcedureDesignator":
            name = n.find_first("Name")
            if name and name.fortran:
                callee = name.fortran.lower()
                break
    args: list[IRExpr] = []
    for arg in node.find_all("ActualArgSpec"):
        expr = arg.find_first("Expr") or next(
            (c for c in arg.children if c.kind != "Keyword"), None
        )
        if expr is not None:
            args.append(_lower_expression(expr))
    cpp_callee = _INTRINSIC_MAP.get(callee, callee)
    return IRFunctionCall(callee=cpp_callee, args=tuple(args))


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
    "Power": "**",         # placeholder — emitter rewrites to std::pow
    "Concat": "+",         # FortranString supports operator+
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
