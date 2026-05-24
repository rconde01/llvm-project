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
from .types import lower_type_spec


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def lower_program(
    root: Node, *, source_file: str | None = None
) -> IRTranslationUnit:
    """Lower an annotated, dependency-ordered parse tree to IR."""
    tu = IRTranslationUnit(source_file=source_file)
    # Find every subprogram in source order — depgraph already did the
    # call-graph sort but for v1 we keep things simple and emit in
    # source-textual order.  We'll plug depgraph back in once we have
    # mutually recursive examples.
    for node in root.walk():
        if node.kind == "MainProgram":
            tu.subprograms.append(_lower_main_program(node))
        elif node.kind == "FunctionSubprogram":
            tu.subprograms.append(_lower_function(node))
        elif node.kind == "SubroutineSubprogram":
            tu.subprograms.append(_lower_subroutine(node))
    return tu


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
    sub.body = [_rename_name_in_stmt(s, sub.name, result_name) for s in sub.body]


def _rename_name_in_stmt(stmt: IRStatement, old: str, new: str) -> IRStatement:
    """Walk an IR statement and substitute every ``IRName(old)`` with ``IRName(new)``."""
    if isinstance(stmt, IRAssignment):
        return IRAssignment(
            target=_rename_name_in_expr(stmt.target, old, new),
            value=_rename_name_in_expr(stmt.value, old, new),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRCall):
        return IRCall(
            callee=stmt.callee,
            args=[_rename_name_in_expr(a, old, new) for a in stmt.args],
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRPrint):
        return IRPrint(
            items=[_rename_name_in_expr(a, old, new) for a in stmt.items],
            stream=stmt.stream,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRReturn):
        if stmt.value is None:
            return stmt
        return IRReturn(
            value=_rename_name_in_expr(stmt.value, old, new),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRIf):
        return IRIf(
            branches=[
                (
                    _rename_name_in_expr(cond, old, new),
                    [_rename_name_in_stmt(s, old, new) for s in body],
                )
                for cond, body in stmt.branches
            ],
            else_body=(
                [_rename_name_in_stmt(s, old, new) for s in stmt.else_body]
                if stmt.else_body is not None
                else None
            ),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRDo):
        return IRDo(
            var=new if stmt.var == old else stmt.var,
            lower=_rename_name_in_expr(stmt.lower, old, new),
            upper=_rename_name_in_expr(stmt.upper, old, new),
            step=(
                _rename_name_in_expr(stmt.step, old, new)
                if stmt.step is not None
                else None
            ),
            body=[_rename_name_in_stmt(s, old, new) for s in stmt.body],
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    return stmt


def _rename_name_in_expr(expr: IRExpr, old: str, new: str) -> IRExpr:
    if isinstance(expr, IRName):
        return IRName(name=new, fortran=expr.fortran) if expr.name == old else expr
    if isinstance(expr, IRBinaryOp):
        return IRBinaryOp(
            op=expr.op,
            lhs=_rename_name_in_expr(expr.lhs, old, new),
            rhs=_rename_name_in_expr(expr.rhs, old, new),
        )
    if isinstance(expr, IRUnaryOp):
        return IRUnaryOp(
            op=expr.op,
            operand=_rename_name_in_expr(expr.operand, old, new),
        )
    if isinstance(expr, IRFunctionCall):
        return IRFunctionCall(
            callee=expr.callee,
            args=tuple(_rename_name_in_expr(a, old, new) for a in expr.args),
        )
    return expr


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
        elif child.kind == "ExecutionPart":
            sub.body.extend(_lower_execution(child))


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def _lower_specification(spec_part: Node) -> list[IRLocal]:
    out: list[IRLocal] = []
    for stmt in spec_part.find_all("TypeDeclarationStmt"):
        out.extend(_lower_type_declaration(stmt))
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
    for attr in decl.find_all("AttrSpec"):
        if attr.source and "PARAMETER" in attr.source.text.upper():
            is_parameter = True
        if attr.source and "SAVE" in attr.source.text.upper():
            is_save = True
        intent_node = attr.find_first("IntentSpec")
        if intent_node is not None:
            intent = _extract_intent(intent_node)

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
        out.append(
            IRLocal(
                name=name_node.fortran.lower(),
                type=ir_type,
                initializer=initializer,
                is_parameter=is_parameter,
                is_save=is_save,
                intent=intent,
            )
        )
    return out


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
    if inner.kind == "CallStmt":
        c = _lower_call(inner)
        c.leading_comments = leading
        c.trailing_comments = trailing
        return c
    if inner.kind == "ReturnStmt":
        r = IRReturn(leading_comments=leading, trailing_comments=trailing)
        return r
    return _unsupported(inner, kind=inner.kind, leading=leading)


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
    return IRPrint(items=items)


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
        return _unsupported(node, kind="DoConstruct (no LoopControl)")

    # Only handle counted bounds for now (the LoopBounds case).
    bounds = loop_control.find_first("LoopBounds")
    if bounds is None:
        return _unsupported(node, kind="DoConstruct (only counted form supported)")

    name = bounds.find_first("Name")
    var = name.fortran.lower() if name and name.fortran else "i"
    exprs = list(bounds.find_all("ScalarIntExpr")) or list(bounds.find_all("Expr"))
    # Expect [lower, upper] or [lower, upper, step].
    lo = _lower_expression(exprs[0]) if exprs else IRRaw("0")
    hi = _lower_expression(exprs[1]) if len(exprs) > 1 else IRRaw("0")
    step = _lower_expression(exprs[2]) if len(exprs) > 2 else None

    body = _lower_block(body_block) if body_block else []
    return IRDo(var=var, lower=lo, upper=hi, step=step, body=body)


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
    if target.kind in _BINARY_OP_MAP or target.kind in _UNARY_OP_MAP:
        return _lower_expr_operator(target)
    return _expr_raw(node)


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
