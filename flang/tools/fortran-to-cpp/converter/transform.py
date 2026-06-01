"""Generic, type-exhaustive IR rewriting helpers.

Several passes need to walk the IR and rewrite it: the lowering pass
renames a function's result variable, and the state-plumbing pass both
rewrites variable references into struct-field accesses and rewrites
call sites to thread state parameters.  Rather than duplicate a
per-statement-type ``match`` in each of those (which has bitten us
every time a new statement kind is added), all structural recursion
lives here.

Two primitives:

  * :func:`map_expr` — bottom-up rewrite of an expression tree.
  * :func:`map_statement` — rebuild a statement, applying an expression
    rewriter to every expression it holds and a statement hook to every
    statement (itself and, recursively, those nested in its blocks).

Adding a new IR statement or expression type means updating *only*
this module.
"""

from __future__ import annotations

from typing import Callable

from .ir import (
    IRAllocate,
    IRArrayConstructor,
    IRAssignment,
    IRBinaryOp,
    IRBlock,
    IRCall,
    IRCaseClause,
    IRComment,
    IRCycle,
    IRDeallocate,
    IRDo,
    IRExit,
    IRExpr,
    IRFunctionCall,
    IRCast,
    IRIf,
    IRImpliedDo,
    IRLambda,
    IRLiteral,
    IRMember,
    IRName,
    IRPointerAssign,
    IRPrint,
    IRRaw,
    IRDirectRead,
    IRDirectWrite,
    IRUnformattedDirectRead,
    IRUnformattedDirectWrite,
    IRInquire,
    IRFilePosition,
    IRRead,
    IRGoto,
    IRReturn,
    IRSection,
    IRSelectCase,
    IRStatement,
    IRStop,
    IRSubstr,
    IRTriplet,
    IRUnaryOp,
    IRUnsupported,
    IRWhile,
)

ExprFn = Callable[[IRExpr], IRExpr]
StmtFn = Callable[[IRStatement], IRStatement]


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


def map_expr(expr: IRExpr, fn: ExprFn) -> IRExpr:
    """Bottom-up rewrite: recurse into children first, then apply ``fn``
    to the rebuilt node.  ``fn`` sees every node in the tree."""
    if isinstance(expr, IRBinaryOp):
        expr = IRBinaryOp(
            op=expr.op,
            lhs=map_expr(expr.lhs, fn),
            rhs=map_expr(expr.rhs, fn),
        )
    elif isinstance(expr, IRUnaryOp):
        expr = IRUnaryOp(op=expr.op, operand=map_expr(expr.operand, fn))
    elif isinstance(expr, IRFunctionCall):
        expr = IRFunctionCall(
            callee=expr.callee,
            args=tuple(map_expr(a, fn) for a in expr.args),
        )
    elif isinstance(expr, IRMember):
        expr = IRMember(base=map_expr(expr.base, fn), field=expr.field)
    elif isinstance(expr, IRSubstr):
        expr = IRSubstr(
            base=map_expr(expr.base, fn),
            lo=map_expr(expr.lo, fn),
            hi=map_expr(expr.hi, fn),
        )
    elif isinstance(expr, IRCast):
        expr = IRCast(cpp_type=expr.cpp_type, operand=map_expr(expr.operand, fn))
    elif isinstance(expr, IRImpliedDo):
        expr = IRImpliedDo(
            var=expr.var,
            lower=map_expr(expr.lower, fn),
            upper=map_expr(expr.upper, fn),
            step=map_expr(expr.step, fn) if expr.step is not None else None,
            items=tuple(map_expr(e, fn) for e in expr.items),
        )
    elif isinstance(expr, IRArrayConstructor):
        expr = IRArrayConstructor(
            elements=tuple(map_expr(e, fn) for e in expr.elements)
        )
    elif isinstance(expr, IRLambda):
        expr = IRLambda(params=expr.params, body=map_expr(expr.body, fn))
    elif isinstance(expr, IRSection):
        new_subs = []
        for s in expr.subscripts:
            if isinstance(s, IRTriplet):
                new_subs.append(
                    IRTriplet(
                        lower=map_expr(s.lower, fn) if s.lower is not None else None,
                        upper=map_expr(s.upper, fn) if s.upper is not None else None,
                        stride=map_expr(s.stride, fn) if s.stride is not None else None,
                    )
                )
            else:
                new_subs.append(map_expr(s, fn))
        expr = IRSection(array=expr.array, subscripts=tuple(new_subs))
    # IRLiteral, IRName, IRRaw are leaves.
    return fn(expr)


def rename_var(expr: IRExpr, old: str, new: str) -> IRExpr:
    """Convenience: substitute every ``IRName(old)`` with ``IRName(new)``."""

    def swap(e: IRExpr) -> IRExpr:
        if isinstance(e, IRName) and e.name == old:
            return IRName(name=new, fortran=e.fortran)
        return e

    return map_expr(expr, swap)


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


def map_statement(
    stmt: IRStatement,
    *,
    on_expr: ExprFn | None = None,
    on_stmt: StmtFn | None = None,
) -> IRStatement:
    """Rebuild ``stmt`` applying ``on_expr`` to every expression it holds
    and recursing into nested statement blocks.

    ``on_stmt``, if given, is applied to every statement *after* its
    children have been rewritten (post-order), so a hook can replace a
    statement and trust that its sub-statements are already transformed.
    """
    rewritten = _map_statement_children(stmt, on_expr, on_stmt)
    if on_stmt is not None:
        return on_stmt(rewritten)
    return rewritten


def map_block(
    body: list[IRStatement],
    *,
    on_expr: ExprFn | None = None,
    on_stmt: StmtFn | None = None,
) -> list[IRStatement]:
    return [map_statement(s, on_expr=on_expr, on_stmt=on_stmt) for s in body]


def _e(expr: IRExpr, on_expr: ExprFn | None) -> IRExpr:
    return on_expr(expr) if on_expr is not None else expr


def _b(
    body: list[IRStatement], on_expr: ExprFn | None, on_stmt: StmtFn | None
) -> list[IRStatement]:
    return [map_statement(s, on_expr=on_expr, on_stmt=on_stmt) for s in body]


def _map_statement_children(
    stmt: IRStatement, on_expr: ExprFn | None, on_stmt: StmtFn | None
) -> IRStatement:
    if isinstance(stmt, IRAssignment):
        return IRAssignment(
            target=_e(stmt.target, on_expr),
            value=_e(stmt.value, on_expr),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRCall):
        return IRCall(
            callee=stmt.callee,
            args=[_e(a, on_expr) for a in stmt.args],
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRPrint):
        return IRPrint(
            items=[_e(a, on_expr) for a in stmt.items],
            stream=stmt.stream,
            format=stmt.format,
            format_expr=(
                _e(stmt.format_expr, on_expr)
                if stmt.format_expr is not None
                else None
            ),
            internal_unit=(
                _e(stmt.internal_unit, on_expr)
                if stmt.internal_unit is not None
                else None
            ),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRRead):
        return IRRead(
            items=[_e(a, on_expr) for a in stmt.items],
            stream=stmt.stream,
            internal_unit=(
                _e(stmt.internal_unit, on_expr)
                if stmt.internal_unit is not None
                else None
            ),
            end_label=stmt.end_label,
            err_label=stmt.err_label,
            iostat_target=(
                _e(stmt.iostat_target, on_expr)
                if stmt.iostat_target is not None
                else None
            ),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRDirectRead):
        return IRDirectRead(
            unit_text=stmt.unit_text,
            rec=_e(stmt.rec, on_expr),
            fields=[
                (_e(tgt, on_expr), kind, off, width, dec)
                for tgt, kind, off, width, dec in stmt.fields
            ],
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRDirectWrite):
        return IRDirectWrite(
            unit_text=stmt.unit_text,
            rec=_e(stmt.rec, on_expr),
            items=[_e(a, on_expr) for a in stmt.items],
            format=stmt.format,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRUnformattedDirectRead):
        return IRUnformattedDirectRead(
            unit_text=stmt.unit_text,
            rec=_e(stmt.rec, on_expr),
            items=[_e(a, on_expr) for a in stmt.items],
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRUnformattedDirectWrite):
        return IRUnformattedDirectWrite(
            unit_text=stmt.unit_text,
            rec=_e(stmt.rec, on_expr),
            items=[_e(a, on_expr) for a in stmt.items],
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRInquire):
        return IRInquire(
            selector_kind=stmt.selector_kind,
            selector=_e(stmt.selector, on_expr),
            outputs=[(f, _e(t, on_expr)) for f, t in stmt.outputs],
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRFilePosition):
        return IRFilePosition(
            op=stmt.op,
            unit=_e(stmt.unit, on_expr),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRStop):
        return IRStop(
            code=_e(stmt.code, on_expr) if stmt.code is not None else None,
            message=stmt.message,
            is_error=stmt.is_error,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRAllocate):
        return IRAllocate(
            obj=stmt.obj,
            extents=[_e(e, on_expr) for e in stmt.extents],
            lowers=[_e(e, on_expr) for e in stmt.lowers],
            cpp_type=stmt.cpp_type,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRGoto):
        return IRGoto(
            target=stmt.target,
            condition=(
                _e(stmt.condition, on_expr) if stmt.condition is not None else None
            ),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRDeallocate):
        return stmt
    if isinstance(stmt, IRPointerAssign):
        return IRPointerAssign(
            pointer=stmt.pointer,
            target=_e(stmt.target, on_expr) if stmt.target is not None else None,
            is_array=stmt.is_array,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRReturn):
        return IRReturn(
            value=_e(stmt.value, on_expr) if stmt.value is not None else None,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRIf):
        return IRIf(
            branches=[
                (_e(cond, on_expr), _b(body, on_expr, on_stmt))
                for cond, body in stmt.branches
            ],
            else_body=(
                _b(stmt.else_body, on_expr, on_stmt)
                if stmt.else_body is not None
                else None
            ),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRDo):
        return IRDo(
            var=stmt.var,
            lower=_e(stmt.lower, on_expr),
            upper=_e(stmt.upper, on_expr),
            step=_e(stmt.step, on_expr) if stmt.step is not None else None,
            body=_b(stmt.body, on_expr, on_stmt),
            declare=stmt.declare,
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRWhile):
        return IRWhile(
            condition=_e(stmt.condition, on_expr),
            body=_b(stmt.body, on_expr, on_stmt),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRSelectCase):
        return IRSelectCase(
            selector=_e(stmt.selector, on_expr),
            clauses=[
                IRCaseClause(
                    values=[_e(v, on_expr) for v in clause.values],
                    ranges=[
                        (
                            _e(lo, on_expr) if lo is not None else None,
                            _e(hi, on_expr) if hi is not None else None,
                        )
                        for lo, hi in clause.ranges
                    ],
                    body=_b(clause.body, on_expr, on_stmt),
                )
                for clause in stmt.clauses
            ],
            default_body=(
                _b(stmt.default_body, on_expr, on_stmt)
                if stmt.default_body is not None
                else None
            ),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    if isinstance(stmt, IRBlock):
        return IRBlock(
            bindings=[(n, _e(v, on_expr)) for n, v in stmt.bindings],
            locals=stmt.locals,
            body=_b(stmt.body, on_expr, on_stmt),
            leading_comments=stmt.leading_comments,
            trailing_comments=stmt.trailing_comments,
        )
    # IRCycle, IRExit, IRComment, IRUnsupported hold no expressions or
    # nested statements.
    return stmt
