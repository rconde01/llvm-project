"""Helpers for the compile-time-lower-bounds array form.

The runtime's ``ftn::Array<T, Rank, Lower>`` and
``ftn::ArrayRef<T, Rank, Lower>`` accept an optional NTTP ``Lower``
that pins the per-dimension lower bound at the type level so indexing
constant-folds the ``(idx - lower)`` subtraction.  These helpers tell
the emitter when a declared array can use that form (every declared
lower bound is a literal integer) and produce the matching type string.

Lives here -- not in ``emit.py`` -- because both the array-local emit
path and IRParameter.cpp_param_decl need it; ``ir.py`` can import this
module but importing ``emit`` would invert the layering.
"""

from __future__ import annotations


def try_static_lower_literals(
    lower_exprs: tuple[str, ...],
) -> tuple[int, ...] | None:
    """Parse a tuple of per-dimension lower-bound expressions; return the
    integer values if every entry is a literal integer (optionally signed,
    optionally paren-wrapped, optionally with a C++ literal suffix), else
    ``None``."""
    if not lower_exprs:
        return None
    values: list[int] = []
    for expr in lower_exprs:
        s = expr.strip()
        while s.startswith("(") and s.endswith(")"):
            inner = s[1:-1].strip()
            if inner.count("(") == inner.count(")"):
                s = inner
            else:
                break
        if s.startswith(("-", "+")):
            sign, body = s[0], s[1:].lstrip()
        else:
            sign, body = "+", s
        while body and body[-1] in "uUlL":
            body = body[:-1]
        if not body.isdigit():
            return None
        values.append(int(sign + body))
    return tuple(values)


def static_lower_nttp(rank: int, lowers: tuple[int, ...]) -> str:
    """Render the ``std::array<ftn::index_t, R>{...}`` NTTP literal."""
    inner = ",".join(str(v) for v in lowers)
    return f"std::array<ftn::index_t, {rank}>{{{inner}}}"


def static_lower_cpp_type(cpp: str, rank: int, lower_exprs: tuple[str, ...]
                          ) -> str | None:
    """Splice the static-``Lower`` NTTP into ``cpp`` (a runtime
    ``ftn::Array<...>`` / ``ftn::ArrayRef<...>`` spelling)
    when every entry in ``lower_exprs`` is a literal integer.
    Returns ``None`` when the runtime form must be used."""
    lbs = try_static_lower_literals(lower_exprs)
    if lbs is None:
        return None
    if len(lbs) != rank:
        return None
    if not cpp.endswith(">"):
        return None
    return cpp[:-1] + f", {static_lower_nttp(rank, lbs)}>"
