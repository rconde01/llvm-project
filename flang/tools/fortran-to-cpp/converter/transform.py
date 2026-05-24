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
    IRAssignment,
    IRBinaryOp,
    IRCall,
    IRCaseClause,
    IRComment,
    IRCycle,
    IRDo,
    IRExit,
    IRExpr,
    IRFunctionCall,
    IRIf,
    IRLiteral,
    IRMember,
    IRName,
    IRPrint,
    IRRaw,
    IRReturn,
    IRSelectCase,
    IRStatement,
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
    # IRCycle, IRExit, IRComment, IRUnsupported hold no expressions or
    # nested statements.
    return stmt
