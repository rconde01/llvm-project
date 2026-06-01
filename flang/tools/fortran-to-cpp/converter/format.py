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

Nested parenthesized groups (``2(1x,f8.2)``) are flattened by repeat.
An unrecognized descriptor raises ``FormatParseError`` — there is no
silent list-directed fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class _Action:
    """One resolved (repeat-expanded) format action."""

    kind: str  # "data" | "space" | "literal" | "newline" | "suppress_nl"
    letter: str = ""  # for "data": I F E D G A L
    width: int | None = None
    decimals: int | None = None
    exp_digits: int | None = None
    count: int = 0  # for "space"
    text: str = ""  # for "literal"
    scale: int = 0  # P scale factor in effect (for E/D/F)


class FormatParseError(ValueError):
    pass


def strip_format(fmt: str) -> str:
    """Remove the surrounding parentheses from a format string."""
    fmt = fmt.strip()
    if fmt.startswith("(") and fmt.endswith(")"):
        return fmt[1:-1]
    return fmt


def parse_format(fmt: str) -> list[_Action]:
    """Parse a Fortran format string into a flat list of actions.

    Handles repeated and nested parenthesized groups (``2(1x,f8.2)``) and
    the ``P`` scale factor (``1pe12.2``).  Raises ``FormatParseError`` on
    an unrecognized descriptor — the caller must surface the error rather
    than silently drop down to list-directed output."""
    actions: list[_Action] = []
    _parse_body(strip_format(fmt), actions, _State())
    return actions


@dataclass
class _State:
    scale: int = 0  # current P scale factor, persists across descriptors


def _parse_body(body: str, actions: list[_Action], state: _State) -> None:
    for token in _split_top_level(body):
        token = token.strip()
        if token:
            _parse_token(token, actions, state)


def _split_top_level(body: str) -> list[str]:
    """Split a format body on commas, respecting quoted literals,
    parenthesized groups (commas inside ``(...)`` do not split), and
    Hollerith descriptors (the old ``5HABCDE`` form, where the digit
    count names the literal length and the next N source characters are
    its text -- regardless of commas / parens inside)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        # Hollerith: ``<digits>H`` consumes the next N source bytes as a
        # literal token (emitted as a quoted string so _parse_token
        # handles it via the literal-text branch).
        if c.isdigit() and depth == 0:
            j = i
            while j < n and body[j].isdigit():
                j += 1
            if j < n and body[j] in ("H", "h"):
                count = int(body[i:j])
                start = j + 1
                end = min(start + count, n)
                text = body[start:end]
                # Flush any pending content (rare; usually the H is the
                # leading descriptor of a fresh token).
                pending = "".join(buf).strip()
                if pending:
                    parts.append(pending)
                buf = []
                parts.append("'" + text.replace("'", "''") + "'")
                i = end
                continue
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
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        if c == "/" and depth == 0:
            # ``/`` is a record terminator and an implicit separator; a
            # leading digit (``2/``) is its repeat count.  Emit any pending
            # descriptor, then the slash-run as its own token.
            pending = "".join(buf).strip()
            prefix = ""
            if pending.isdigit():
                prefix = pending
            elif pending:
                parts.append(pending)
            buf = []
            run = ""
            while i < n and body[i] == "/":
                run += "/"
                i += 1
            parts.append(prefix + run)
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


_GROUP_RE = re.compile(r"^(?P<repeat>\d+)?\((?P<body>.*)\)$", re.DOTALL)
_SCALE_RE = re.compile(r"^(?P<scale>[+-]?\d+)[pP](?P<rest>.*)$", re.DOTALL)


def _parse_token(token: str, actions: list[_Action], state: _State) -> None:
    # Repeated / nested group: ``2(...)`` or ``(...)``.
    g = _GROUP_RE.match(token)
    if g is not None:
        repeat = int(g.group("repeat")) if g.group("repeat") else 1
        for _ in range(repeat):
            _parse_body(g.group("body"), actions, state)
        return
    # Quoted literal text.
    if token[0] in ("'", '"'):
        quote = token[0]
        inner = token[1:-1] if token.endswith(quote) else token[1:]
        inner = inner.replace(quote + quote, quote)
        actions.append(_Action(kind="literal", text=inner))
        return
    # Slash = record terminator (newline): ``/`` (1), ``//`` (2), or a
    # repeated ``n/`` (n).
    sl = re.match(r"^(\d+)?(/+)$", token)
    if sl is not None:
        repeat = int(sl.group(1)) if sl.group(1) else 1
        for _ in range(repeat * len(sl.group(2))):
            actions.append(_Action(kind="newline"))
        return
    # ``kP`` scale factor — persists for subsequent E/D/F; may be glued to
    # a descriptor (``1pe12.2``).
    sc = _SCALE_RE.match(token)
    if sc is not None:
        state.scale = int(sc.group("scale"))
        rest = sc.group("rest").strip()
        if rest:
            _parse_token(rest, actions, state)
        return
    if token == "$":
        # Non-standard "suppress trailing newline" marker (used for
        # prompts).  Caller honours this by omitting the ``<< '\n'`` at
        # the end of the formatted print.
        actions.append(_Action(kind="suppress_nl"))
        return
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
        actions.append(_Action(kind="space", count=count))
        return
    if letter in ("I", "F", "E", "D", "G", "A", "L"):
        for _ in range(repeat):
            actions.append(
                _Action(
                    kind="data",
                    letter=letter,
                    width=width,
                    decimals=dec,
                    exp_digits=exp,
                    scale=state.scale,
                )
            )
        return
    raise FormatParseError(f"unsupported descriptor letter {letter!r}")


def render_format(
    fmt: str, item_exprs: list[str]
) -> tuple[list[str], bool]:
    """Map a format + rendered output-item expressions to C++ chunks.

    Returns ``(chunks, suppress_trailing_newline)``: the chunks are the
    C++ expressions to join with ``<<``; the flag is ``True`` iff the
    format ended with a ``$`` (non-standard "no newline" marker used for
    interactive prompts) so the caller knows to omit its own ``<< '\\n'``.
    """
    actions = parse_format(fmt)
    chunks: list[str] = []
    suppress_nl = False
    item_iter = iter(item_exprs)
    for act in actions:
        if act.kind == "literal":
            chunks.append(_cpp_string_literal(act.text))
        elif act.kind == "space":
            chunks.append(_cpp_string_literal(" " * act.count))
        elif act.kind == "newline":
            chunks.append("'\\n'")
        elif act.kind == "suppress_nl":
            suppress_nl = True
        elif act.kind == "data":
            try:
                item = next(item_iter)
            except StopIteration:
                # More descriptors than items: stop (Fortran would
                # terminate the record here).
                break
            chunks.append(_render_data(act, item))
    return chunks, suppress_nl


def _render_data(act: _Action, item: str) -> str:
    letter = act.letter
    w = act.width
    d = act.decimals
    if letter == "I":
        spec = f"{{:{w}d}}" if w is not None else "{}"
        return f'std::format("{spec}", {item})'
    if letter == "F":
        if act.scale and w is not None and d is not None:
            return f"fortran::io::fmt_F_with_scale({item}, {act.scale}, {w}, {d})"
        if w is not None and d is not None:
            # ``#`` keeps a trailing decimal point when d=0 — Fortran F
            # always prints the radix point, but C++ std::format with
            # ``.0f`` drops it without the alternate-form flag.
            return f'std::format("{{:#{w}.{d}f}}", {item})'
        return f'std::format("{{}}", {item})'
    if letter == "A":
        # ``fortran::io::fmt_A`` reinterprets a numeric item's bytes as a
        # character buffer (Fortran semantics for A applied to INTEGER /
        # REAL items holding packed character data — common in F77).
        # For genuine character items it does the right-justify /
        # truncate behavior of the Aw descriptor.
        if w is not None:
            return f"fortran::io::fmt_A({item}, {w})"
        return f"fortran::io::fmt_A_default({item})"
    if letter == "L":
        # Fortran prints logicals right-justified as T / F.
        w_arg = w if w is not None else 1
        return f'std::format("{{:>{w_arg}}}", ({item}) ? "T" : "F")'
    if letter in ("E", "D"):
        ww = w if w is not None else 15
        dd = d if d is not None else 6
        exp = act.exp_digits
        exp_arg = f", {exp}" if exp is not None else ""
        if act.scale:
            return (
                f"fortran::io::fmt_E_with_scale("
                f"{item}, {act.scale}, {ww}, {dd}{exp_arg})"
            )
        return f"fortran::io::fmt_E({item}, {ww}, {dd}{exp_arg})"
    if letter == "G":
        ww = w if w is not None else 15
        dd = d if d is not None else 6
        return f"fortran::io::fmt_G({item}, {ww}, {dd})"
    raise FormatParseError(f"unsupported descriptor letter {letter!r}")


def _cpp_string_literal(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"sv'
