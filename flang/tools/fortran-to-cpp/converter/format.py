"""Fortran FORMAT string -> C++ output expression chunks.

Parses a Fortran edit-descriptor list and, given the already-rendered
C++ text of each output item, returns a list of C++ expressions to be
joined with ``<<`` on the output stream (see emit.py).  Per decision
D5 / rule R8:

  * descriptors with a clean ``std::format`` analogue (I, F, A, L, X,
    literals, ``/``) become inline ``std::format`` calls or string
    literals — readable plain C++;
  * descriptors that need byte-fidelity (E, D, G) become calls into
    the ``fortran::io`` runtime helpers.

Only a flat descriptor list is handled for now; nested parenthesized
groups fall back to a passthrough that emits the items list-directed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class _Action:
    """One resolved (repeat-expanded) format action."""

    kind: str  # "data" | "space" | "literal" | "newline"
    letter: str = ""  # for "data": I F E D G A L
    width: int | None = None
    decimals: int | None = None
    exp_digits: int | None = None
    count: int = 0  # for "space"
    text: str = ""  # for "literal"


class FormatParseError(ValueError):
    pass


def strip_format(fmt: str) -> str:
    """Remove the surrounding parentheses from a format string."""
    fmt = fmt.strip()
    if fmt.startswith("(") and fmt.endswith(")"):
        return fmt[1:-1]
    return fmt


def parse_format(fmt: str) -> list[_Action]:
    """Parse a (flat) Fortran format string into a list of actions.

    Raises FormatParseError on a nested group or an unrecognized
    descriptor so the caller can fall back to list-directed output.
    """
    body = strip_format(fmt)
    if "(" in body or ")" in body:
        raise FormatParseError("nested format groups are not supported yet")
    actions: list[_Action] = []
    for token in _split_top_level(body):
        token = token.strip()
        if not token:
            continue
        actions.extend(_parse_token(token))
    return actions


def _split_top_level(body: str) -> list[str]:
    """Split a format body on commas, respecting quoted literals."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c in ("'", '"'):
            # Consume the whole quoted literal (with doubled-quote escapes).
            quote = c
            buf.append(c)
            i += 1
            while i < n:
                buf.append(body[i])
                if body[i] == quote:
                    if i + 1 < n and body[i + 1] == quote:
                        buf.append(body[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == ",":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


_DESCRIPTOR_RE = re.compile(
    r"^(?P<repeat>\d+)?"
    r"(?P<letter>[A-Za-z]+)"
    r"(?P<width>\d+)?"
    r"(?:\.(?P<dec>\d+))?"
    r"(?:[eE](?P<exp>\d+))?$"
)


def _parse_token(token: str) -> list[_Action]:
    # Quoted literal text.
    if token[0] in ("'", '"'):
        quote = token[0]
        inner = token[1:-1] if token.endswith(quote) else token[1:]
        inner = inner.replace(quote + quote, quote)
        return [_Action(kind="literal", text=inner)]
    # Slash = record terminator (newline), possibly repeated (``2/``).
    if set(token) <= {"/"}:
        return [_Action(kind="newline") for _ in token]
    m = _DESCRIPTOR_RE.match(token)
    if not m:
        raise FormatParseError(f"unrecognized descriptor {token!r}")
    repeat = int(m.group("repeat")) if m.group("repeat") else 1
    letter = m.group("letter").upper()
    width = int(m.group("width")) if m.group("width") else None
    dec = int(m.group("dec")) if m.group("dec") else None
    exp = int(m.group("exp")) if m.group("exp") else None

    if letter == "X":
        # ``nX`` -> n spaces.  The leading number is the count, not a
        # repeat; default 1.
        count = int(m.group("repeat")) if m.group("repeat") else 1
        return [_Action(kind="space", count=count)]
    if letter in ("I", "F", "E", "D", "G", "A", "L"):
        return [
            _Action(
                kind="data",
                letter=letter,
                width=width,
                decimals=dec,
                exp_digits=exp,
            )
            for _ in range(repeat)
        ]
    if letter in ("P",):
        # Scale factor — affects the next F/E.  Not handled yet.
        raise FormatParseError("P scale factor not supported yet")
    raise FormatParseError(f"unsupported descriptor letter {letter!r}")


def render_format(fmt: str, item_exprs: list[str]) -> list[str]:
    """Map a format + rendered output-item expressions to C++ chunks.

    Returns the list of C++ expressions to join with ``<<``.  A
    trailing newline is *not* added here — the caller appends it.
    """
    actions = parse_format(fmt)
    chunks: list[str] = []
    item_iter = iter(item_exprs)
    for act in actions:
        if act.kind == "literal":
            chunks.append(_cpp_string_literal(act.text))
        elif act.kind == "space":
            chunks.append(_cpp_string_literal(" " * act.count))
        elif act.kind == "newline":
            chunks.append("'\\n'")
        elif act.kind == "data":
            try:
                item = next(item_iter)
            except StopIteration:
                # More descriptors than items: stop (Fortran would
                # terminate the record here).
                break
            chunks.append(_render_data(act, item))
    return chunks


def _render_data(act: _Action, item: str) -> str:
    letter = act.letter
    w = act.width
    d = act.decimals
    if letter == "I":
        spec = f"{{:{w}d}}" if w is not None else "{}"
        return f'std::format("{spec}", {item})'
    if letter == "F":
        if w is not None and d is not None:
            return f'std::format("{{:{w}.{d}f}}", {item})'
        return f'std::format("{{}}", {item})'
    if letter == "A":
        spec = f"{{:>{w}}}" if w is not None else "{}"
        return f'std::format("{spec}", {item})'
    if letter == "L":
        # Fortran prints logicals right-justified as T / F.
        w_arg = w if w is not None else 1
        return f'std::format("{{:>{w_arg}}}", ({item}) ? "T" : "F")'
    if letter in ("E", "D"):
        ww = w if w is not None else 15
        dd = d if d is not None else 6
        exp = act.exp_digits
        exp_arg = f", {exp}" if exp is not None else ""
        return f"fortran::io::fmt_E({item}, {ww}, {dd}{exp_arg})"
    if letter == "G":
        ww = w if w is not None else 15
        dd = d if d is not None else 6
        return f"fortran::io::fmt_G({item}, {ww}, {dd})"
    return f'std::format("{{}}", {item})'


def _cpp_string_literal(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"sv'
