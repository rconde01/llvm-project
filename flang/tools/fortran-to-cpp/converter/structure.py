"""Structuring pass: eliminate ``goto`` in favor of ordinary control flow.

The lowering pass leaves ``goto`` as transient :class:`IRGoto` markers and
labeled points as :class:`IRLabel` markers.  This pass rewrites them into
structured C++ control flow with **no goto keyword**:

  * Constructs that no goto crosses are kept as-is (their inner bodies are
    structured recursively), so clean loops and ifs stay clean.
  * The common forward ``if (c) goto L`` skip idiom becomes an ``if`` block.
  * Anything left (backward jumps, irreducible spaghetti) is lowered to a
    correct, goto-free dispatch loop — ``while (_pc != DONE) switch (_pc)``
    — built entirely from existing IR (while / select / assignment).

Only procedures that actually contain a goto/label are touched; everything
else is returned unchanged.
"""

from __future__ import annotations

from collections import Counter

from .ir import (
    IRAssignment,
    IRBinaryOp,
    IRBlock,
    IRCaseClause,
    IRCycle,
    IRDo,
    IRExit,
    IRExpr,
    IRGoto,
    IRIf,
    IRLabel,
    IRLiteral,
    IRName,
    IRReturn,
    IRSelectCase,
    IRStatement,
    IRStop,
    IRUnaryOp,
    IRWhile,
)

_PC = "_pc"  # dispatch state variable


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def structure_gotos(body: list[IRStatement]) -> tuple[list[IRStatement], bool]:
    """Return ``(structured_body, used_dispatch)``.

    ``used_dispatch`` is True when a dispatch loop was emitted, so the
    caller knows to declare the ``_pc`` state local."""
    if not _has_control(body):
        return body, False
    total: Counter = Counter()
    _collect_targets(body, total)
    fresh = _Fresh()
    flat: list[IRStatement] = []
    _flatten(body, flat, fresh, [], total)
    return _structure_flat(flat)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Fresh:
    def __init__(self) -> None:
        self.n = 1_000_000

    def __call__(self) -> int:
        self.n += 1
        return self.n


def _child_bodies(stmt: IRStatement) -> list[list[IRStatement]]:
    if isinstance(stmt, IRIf):
        out = [body for _, body in stmt.branches]
        if stmt.else_body is not None:
            out.append(stmt.else_body)
        return out
    if isinstance(stmt, (IRDo, IRWhile, IRBlock)):
        return [stmt.body]
    if isinstance(stmt, IRSelectCase):
        out = [c.body for c in stmt.clauses]
        if stmt.default_body is not None:
            out.append(stmt.default_body)
        return out
    return []


def _has_control(stmts: list[IRStatement]) -> bool:
    for s in stmts:
        if isinstance(s, (IRLabel, IRGoto)):
            return True
        if any(_has_control(b) for b in _child_bodies(s)):
            return True
    return False


def _collect_targets(stmts: list[IRStatement], acc: Counter) -> None:
    for s in stmts:
        if isinstance(s, IRGoto):
            acc[s.target] += 1
        for b in _child_bodies(s):
            _collect_targets(b, acc)


def _collect_labels(stmts: list[IRStatement], acc: set[int]) -> None:
    for s in stmts:
        if isinstance(s, IRLabel):
            acc.add(s.label)
        for b in _child_bodies(s):
            _collect_labels(b, acc)


def _self_contained(stmt: IRStatement, total_targets: Counter) -> bool:
    """True if no goto crosses ``stmt``'s boundary in either direction.

    ``total_targets`` counts goto targets over the *entire* procedure body."""
    inner_targets: Counter = Counter()
    inner_labels: set[int] = set()
    for b in _child_bodies(stmt):
        _collect_targets(b, inner_targets)
        _collect_labels(b, inner_labels)
    # A goto inside escapes if its target isn't defined inside.
    if any(t not in inner_labels for t in inner_targets):
        return False
    # A label inside is targeted from outside if the whole-body count
    # exceeds the count of references that live inside this construct.
    for lbl in inner_labels:
        if total_targets[lbl] > inner_targets[lbl]:
            return False
    return True


def _binop(op: str, lhs: IRExpr, rhs: IRExpr) -> IRExpr:
    return IRBinaryOp(op=op, lhs=lhs, rhs=rhs)


def _not(cond: IRExpr) -> IRExpr:
    return IRUnaryOp(op="!", operand=IRUnaryOp(op="()", operand=cond))


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def _flatten(
    stmts: list[IRStatement],
    out: list[IRStatement],
    fresh: "_Fresh",
    loopctx: list[tuple[int, int]],
    total: Counter,
) -> None:
    for s in stmts:
        _flatten_one(s, out, fresh, loopctx, total)


def _flatten_one(
    s: IRStatement,
    out: list[IRStatement],
    fresh: "_Fresh",
    loopctx: list[tuple[int, int]],
    total: Counter,
) -> None:
    if isinstance(s, (IRLabel, IRGoto)):
        out.append(s)
        return
    if isinstance(s, IRCycle):
        out.append(IRGoto(target=loopctx[-1][0]) if loopctx else s)
        return
    if isinstance(s, IRExit):
        out.append(IRGoto(target=loopctx[-1][1]) if loopctx else s)
        return
    if isinstance(s, (IRIf, IRDo, IRWhile, IRSelectCase, IRBlock)):
        if _self_contained(s, total):
            out.append(_structure_in_place(s))
        else:
            _flatten_construct(s, out, fresh, loopctx, total)
        return
    out.append(s)


def _flatten_construct(
    s: IRStatement,
    out: list[IRStatement],
    fresh: "_Fresh",
    loopctx: list[tuple[int, int]],
    total: Counter,
) -> None:
    if isinstance(s, IRIf):
        end = fresh()
        for cond, body in s.branches:
            nxt = fresh()
            out.append(IRGoto(target=nxt, condition=_not(cond)))
            _flatten(body, out, fresh, loopctx, total)
            out.append(IRGoto(target=end))
            out.append(IRLabel(label=nxt))
        if s.else_body is not None:
            _flatten(s.else_body, out, fresh, loopctx, total)
        out.append(IRLabel(label=end))
        return
    if isinstance(s, IRWhile):
        top, end = fresh(), fresh()
        out.append(IRLabel(label=top))
        out.append(IRGoto(target=end, condition=_not(s.condition)))
        _flatten(s.body, out, fresh, loopctx + [(top, end)], total)
        out.append(IRGoto(target=top))
        out.append(IRLabel(label=end))
        return
    if isinstance(s, IRDo):
        top, cont, end = fresh(), fresh(), fresh()
        var = IRName(name=s.var, fortran=s.var)
        out.append(IRAssignment(target=var, value=s.lower))
        out.append(IRLabel(label=top))
        out.append(IRGoto(target=end, condition=_not(_do_test(s))))
        _flatten(s.body, out, fresh, loopctx + [(cont, end)], total)
        out.append(IRLabel(label=cont))
        step = s.step if s.step is not None else IRLiteral(cpp_text="1")
        out.append(IRAssignment(target=var, value=_binop("+", var, step)))
        out.append(IRGoto(target=top))
        out.append(IRLabel(label=end))
        return
    if isinstance(s, IRSelectCase):
        end = fresh()
        for clause in s.clauses:
            nxt = fresh()
            out.append(
                IRGoto(target=nxt, condition=_not(_clause_cond(s.selector, clause)))
            )
            _flatten(clause.body, out, fresh, loopctx, total)
            out.append(IRGoto(target=end))
            out.append(IRLabel(label=nxt))
        if s.default_body is not None:
            _flatten(s.default_body, out, fresh, loopctx, total)
        out.append(IRLabel(label=end))
        return
    if isinstance(s, IRBlock):
        _flatten(s.body, out, fresh, loopctx, total)
        return
    out.append(s)


def _do_test(s: IRDo) -> IRExpr:
    var = IRName(name=s.var, fortran=s.var)
    if s.step is None:
        return _binop("<=", var, s.upper)
    neg = (
        isinstance(s.step, IRLiteral)
        and s.step.cpp_text.startswith("-")
    )
    return _binop(">=" if neg else "<=", var, s.upper)


def _clause_cond(selector: IRExpr, clause: IRCaseClause) -> IRExpr:
    parts: list[IRExpr] = [_binop("==", selector, v) for v in clause.values]
    for lo, hi in clause.ranges:
        conds: list[IRExpr] = []
        if lo is not None:
            conds.append(_binop(">=", selector, lo))
        if hi is not None:
            conds.append(_binop("<=", selector, hi))
        if conds:
            c = conds[0]
            for extra in conds[1:]:
                c = _binop("&&", c, extra)
            parts.append(IRUnaryOp(op="()", operand=c))
    if not parts:
        return IRLiteral(cpp_text="true")
    cond = parts[0]
    for extra in parts[1:]:
        cond = _binop("||", cond, extra)
    return cond


def _structure_in_place(stmt: IRStatement) -> IRStatement:
    """Recursively structure a self-contained construct's inner bodies."""
    if isinstance(stmt, IRIf):
        stmt.branches = [
            (cond, structure_gotos(body)[0]) for cond, body in stmt.branches
        ]
        if stmt.else_body is not None:
            stmt.else_body = structure_gotos(stmt.else_body)[0]
    elif isinstance(stmt, (IRDo, IRWhile, IRBlock)):
        stmt.body = structure_gotos(stmt.body)[0]
    elif isinstance(stmt, IRSelectCase):
        for c in stmt.clauses:
            c.body = structure_gotos(c.body)[0]
        if stmt.default_body is not None:
            stmt.default_body = structure_gotos(stmt.default_body)[0]
    return stmt


# ---------------------------------------------------------------------------
# Structuring the flat list
# ---------------------------------------------------------------------------


def _structure_flat(flat: list[IRStatement]) -> tuple[list[IRStatement], bool]:
    pretty = _peephole(list(flat))
    if not any(isinstance(s, IRGoto) for s in pretty):
        return [s for s in pretty if not isinstance(s, IRLabel)], False
    return _dispatch(flat), True


def _peephole(flat: list[IRStatement]) -> list[IRStatement]:
    """Fold the forward conditional-skip idiom into ``if`` blocks."""
    changed = True
    while changed:
        changed = False
        targets: Counter = Counter()
        _collect_targets(flat, targets)
        new = [
            s for s in flat
            if not (isinstance(s, IRLabel) and targets[s.label] == 0)
        ]
        if len(new) != len(flat):
            flat = new
            changed = True
            continue
        for i, s in enumerate(flat):
            if not (isinstance(s, IRGoto) and s.condition is not None):
                continue
            j = _find_label(flat, s.target, i + 1)
            if j is None:
                continue
            span = flat[i + 1 : j]
            if any(isinstance(x, (IRGoto, IRLabel)) for x in span):
                continue
            block = IRIf(branches=[(_not(s.condition), span)], else_body=None)
            flat = flat[:i] + [block] + flat[j:]
            changed = True
            break
    return flat


def _find_label(flat: list[IRStatement], label: int, start: int) -> int | None:
    for k in range(start, len(flat)):
        s = flat[k]
        if isinstance(s, IRLabel) and s.label == label:
            return k
    return None


def _dispatch(flat: list[IRStatement]) -> list[IRStatement]:
    """Lower the flat list to a goto-free ``while``/``switch`` dispatch loop."""
    segments: list[tuple[int | None, list[IRStatement]]] = []
    cur: list[IRStatement] = []
    lbl: int | None = None
    for s in flat:
        if isinstance(s, IRLabel):
            segments.append((lbl, cur))
            cur = []
            lbl = s.label
        else:
            cur.append(s)
    segments.append((lbl, cur))

    n = len(segments)
    done = n
    label_to_state = {
        seg_lbl: idx
        for idx, (seg_lbl, _) in enumerate(segments)
        if seg_lbl is not None
    }

    def state_of(target: int) -> int:
        return label_to_state.get(target, done)

    def set_pc(value: int) -> IRStatement:
        return IRAssignment(
            target=IRName(name=_PC, fortran=_PC),
            value=IRLiteral(cpp_text=str(value)),
        )

    clauses: list[IRCaseClause] = []
    for idx, (_, ops) in enumerate(segments):
        body: list[IRStatement] = []
        terminated = False
        for op in ops:
            if isinstance(op, IRGoto) and op.condition is None:
                body.append(set_pc(state_of(op.target)))
                terminated = True
                break
            if isinstance(op, IRGoto):
                body.append(
                    IRIf(
                        branches=[
                            (op.condition, [set_pc(state_of(op.target)), IRCycle()])
                        ],
                        else_body=None,
                    )
                )
                continue
            if isinstance(op, (IRReturn, IRStop)):
                body.append(op)
                terminated = True
                break
            body.append(op)
        if not terminated:
            body.append(set_pc(idx + 1 if idx + 1 < n else done))
        clauses.append(
            IRCaseClause(values=[IRLiteral(cpp_text=str(idx))], body=body)
        )

    loop = IRWhile(
        condition=_binop(
            "!=", IRName(name=_PC, fortran=_PC), IRLiteral(cpp_text=str(done))
        ),
        body=[IRSelectCase(selector=IRName(name=_PC, fortran=_PC), clauses=clauses)],
    )
    return [set_pc_initial(), loop]


def set_pc_initial() -> IRStatement:
    return IRAssignment(
        target=IRName(name=_PC, fortran=_PC), value=IRLiteral(cpp_text="0")
    )
