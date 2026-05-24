"""Order Fortran subprograms by their call dependencies.

Given a parse tree, this module finds every callable unit
(``MainProgram``, ``FunctionSubprogram``, ``SubroutineSubprogram`` —
including those nested in modules and ``CONTAINS`` blocks) and returns
them in *callee-first* topological order:

    For any A that calls B, B appears before A in the output.

Mutually recursive groups (cycles in the call graph) are kept together;
within a group, items are sorted by source location.  The algorithm is
Tarjan's SCC, which naturally yields strongly-connected components in
reverse topological order.

CLI:

    python -m flang_ast.depgraph hello.f90          # names, one per line
    python -m flang_ast.depgraph hello.f90 --json   # full structured output
    python -m flang_ast.depgraph hello.f90 --dot    # Graphviz dot file
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .nodes import Node, SourceRange
from .parser import parse_json_file
from .runner import parse_fortran_file


# ---------------------------------------------------------------------------
# Subprogram model
# ---------------------------------------------------------------------------


# Node kinds that name a callable unit.
_SUBPROGRAM_KINDS: frozenset[str] = frozenset(
    {
        "MainProgram",
        "FunctionSubprogram",
        "SubroutineSubprogram",
    }
)

# Statement nodes whose first ``Name`` child gives the unit's name.
_NAMING_STMTS: frozenset[str] = frozenset(
    {
        "ProgramStmt",
        "FunctionStmt",
        "SubroutineStmt",
    }
)


@dataclass(slots=True)
class Subprogram:
    """A single callable unit extracted from the parse tree."""

    name: str
    """Canonical (lower-cased) name used for graph lookups."""

    display_name: str
    """Name as it appears in source / in ``fortran`` (after sema, uppercased)."""

    kind: str
    """``"main"``, ``"function"``, or ``"subroutine"``."""

    node: Node
    """The owning parse tree node (``MainProgram`` or ``*Subprogram``)."""

    source: SourceRange | None
    """Best-effort source range for the subprogram header."""

    calls: list[str] = field(default_factory=list)
    """Canonical names of subprograms this one calls, in source order."""

    external_calls: list[str] = field(default_factory=list)
    """Names referenced as calls that do not resolve to a known subprogram
    (typically intrinsics, externals, or routines imported via ``USE``)."""

    parent: str | None = None
    """Canonical name of the enclosing subprogram for internal (``CONTAINS``)
    subprograms; ``None`` for top-level units."""

    @property
    def line(self) -> int:
        """Convenience: 1-based start line, or 0 if unknown."""
        if self.source is None or self.source.line is None:
            return 0
        return self.source.line

    def source_text(self) -> str:
        """Return the original source text for this subprogram, if available."""
        if (
            self.source is None
            or self.source.file is None
            or self.source.line is None
        ):
            return ""
        # The Statement source on the header only covers the header line.
        # Read the whole subprogram by union of its descendants' source ranges.
        first_line, last_line = _line_span(self.node)
        if first_line is None or last_line is None:
            return ""
        try:
            text = Path(self.source.file).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""
        lines = text.splitlines(keepends=True)
        return "".join(lines[first_line - 1 : last_line])

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "name": self.name,
            "displayName": self.display_name,
            "kind": self.kind,
            "calls": list(self.calls),
            "externalCalls": list(self.external_calls),
        }
        if self.parent is not None:
            out["parent"] = self.parent
        if self.source is not None:
            out["source"] = self.source.to_json()
        return out


@dataclass(slots=True)
class OrderingResult:
    """Result of dependency-ordering a parse tree."""

    order: list[Subprogram]
    """Subprograms in callee-first topological order."""

    sccs: list[list[Subprogram]]
    """Each strongly-connected component, in the same global order.

    Singleton SCCs (the common case) hold one element; multi-element
    SCCs represent groups of mutually-recursive subprograms.
    """

    by_name: dict[str, Subprogram]
    """All subprograms keyed by canonical name (lower-cased)."""

    @property
    def cycles(self) -> list[list[Subprogram]]:
        """SCCs with more than one element (i.e., actual cycles)."""
        return [scc for scc in self.sccs if len(scc) > 1]

    def to_json(self) -> dict[str, object]:
        return {
            "order": [s.name for s in self.order],
            "subprograms": [s.to_json() for s in self.order],
            "cycles": [[s.name for s in scc] for scc in self.cycles],
        }


# ---------------------------------------------------------------------------
# Subprogram discovery
# ---------------------------------------------------------------------------


def _name_from_header(node: Node) -> str | None:
    """Pull a subprogram's declared name out of its header statement."""
    # Look at the *first* Statement child (the header), then drill in.
    # ``Name`` nodes carry the spelling via ``fortran``.
    for stmt in node.children:
        if stmt.kind != "Statement":
            continue
        for sub in stmt.walk():
            if sub.kind in _NAMING_STMTS:
                for name_node in sub.walk():
                    if name_node.kind == "Name" and name_node.fortran:
                        return name_node.fortran
                return None
        # Some MainPrograms have no ProgramStmt at all; bail out after
        # consuming the first Statement.
        return None
    return None


def _header_source(node: Node) -> SourceRange | None:
    """Return the source range covering the header statement, if any."""
    for stmt in node.children:
        if stmt.kind == "Statement" and stmt.source is not None:
            return stmt.source
    return node.source


def _kind_label(node_kind: str) -> str:
    if node_kind == "MainProgram":
        return "main"
    if node_kind == "FunctionSubprogram":
        return "function"
    if node_kind == "SubroutineSubprogram":
        return "subroutine"
    return node_kind


def collect_subprograms(root: Node) -> list[Subprogram]:
    """Find every callable unit in ``root``, in source order.

    Nested (``CONTAINS``) subprograms get their own entries with
    ``parent`` set to the enclosing unit's canonical name.
    """
    out: list[Subprogram] = []
    _collect_subprograms_rec(root, parent=None, out=out)
    out.sort(key=lambda s: (s.line, s.name))
    return out


def _collect_subprograms_rec(
    node: Node, *, parent: str | None, out: list[Subprogram]
) -> None:
    if node.kind in _SUBPROGRAM_KINDS:
        display = _name_from_header(node) or f"<anon:{node.kind}>"
        sub = Subprogram(
            name=display.lower(),
            display_name=display,
            kind=_kind_label(node.kind),
            node=node,
            source=_header_source(node),
            parent=parent,
        )
        out.append(sub)
        # Recurse into the body but treat nested subprograms as having
        # *this* unit as parent.  Skip the header Statement to avoid
        # re-finding the name.
        for child in node.children:
            _collect_subprograms_rec(child, parent=sub.name, out=out)
        return
    for child in node.children:
        _collect_subprograms_rec(child, parent=parent, out=out)


# ---------------------------------------------------------------------------
# Call discovery
# ---------------------------------------------------------------------------


# Nodes whose first ``Name`` descendant identifies a callee.
_CALL_NODES: frozenset[str] = frozenset(
    {
        "CallStmt",        # subroutine invocation
        "FunctionReference",  # function call inside an expression
    }
)


def _first_name_in(node: Node) -> str | None:
    for n in node.walk():
        if n.kind == "Name" and n.fortran:
            return n.fortran
    return None


def _walk_body_calls(sub: Subprogram) -> Iterator[str]:
    """Yield callee names referenced from ``sub``'s body, in source order.

    Descent stops at nested ``CONTAINS`` subprograms; their calls belong
    to those subprograms, not to this one.
    """
    stack: list[tuple[Node, bool]] = [(sub.node, True)]
    while stack:
        node, is_root = stack.pop()
        if not is_root and node.kind in _SUBPROGRAM_KINDS:
            continue  # nested subprogram boundary
        if node.kind in _CALL_NODES:
            name = _first_name_in(node)
            if name:
                yield name
        # Children pushed in reverse so we visit in source order.
        for child in reversed(node.children):
            stack.append((child, False))


# ---------------------------------------------------------------------------
# Topological order (Tarjan's SCC algorithm)
# ---------------------------------------------------------------------------


def _tarjan_scc(
    nodes: list[str], edges: dict[str, list[str]]
) -> list[list[str]]:
    """Return the SCCs of (``nodes``, ``edges``) in reverse topo order.

    Iterative implementation that handles graphs deeper than Python's
    recursion limit.  Within each SCC, nodes are returned in the order
    they were popped from the stack, which is a stable order across
    runs given a stable input order.
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    for start in nodes:
        if start in index:
            continue
        work: list[tuple[str, Iterator[str]]] = [
            (start, iter(edges.get(start, ())))
        ]
        index[start] = index_counter[0]
        lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)

        while work:
            v, succ = work[-1]
            try:
                w = next(succ)
            except StopIteration:
                work.pop()
                if work:
                    parent_v = work[-1][0]
                    lowlink[parent_v] = min(lowlink[parent_v], lowlink[v])
                if lowlink[v] == index[v]:
                    component: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == v:
                            break
                    result.append(component)
                continue
            if w not in index:
                index[w] = index_counter[0]
                lowlink[w] = index_counter[0]
                index_counter[0] += 1
                stack.append(w)
                on_stack.add(w)
                work.append((w, iter(edges.get(w, ()))))
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

    return result


def order_by_dependencies(root: Node) -> OrderingResult:
    """Build the call graph and topologically sort it (callees first).

    Resolution is by name only and ignores ``USE`` renaming / generic
    interfaces / pointer calls.  Anything that resolves to a known
    subprogram becomes an in-graph dependency; anything else is
    recorded in ``Subprogram.external_calls``.
    """
    subs = collect_subprograms(root)
    by_name: dict[str, Subprogram] = {}
    for s in subs:
        # Inner units shadow outer ones of the same name when running
        # inside the same parent; for the global graph we keep the
        # earliest-seen entry.
        by_name.setdefault(s.name, s)

    edges: dict[str, list[str]] = {s.name: [] for s in subs}
    for s in subs:
        seen_in_this: set[str] = set()
        for raw in _walk_body_calls(s):
            canonical = raw.lower()
            if canonical == s.name:
                continue  # ignore direct self-recursion in edges
            if canonical in by_name:
                if canonical not in seen_in_this:
                    s.calls.append(canonical)
                    edges[s.name].append(canonical)
                    seen_in_this.add(canonical)
            else:
                if raw not in s.external_calls:
                    s.external_calls.append(raw)

    sccs_canon = _tarjan_scc([s.name for s in subs], edges)
    sccs: list[list[Subprogram]] = []
    order: list[Subprogram] = []
    for component in sccs_canon:
        members = sorted(
            (by_name[n] for n in component if n in by_name),
            key=lambda s: (s.line, s.name),
        )
        if members:
            sccs.append(members)
            order.extend(members)

    return OrderingResult(order=order, sccs=sccs, by_name=by_name)


# ---------------------------------------------------------------------------
# Convenience: shortest-path style helpers (useful for converters)
# ---------------------------------------------------------------------------


def iter_in_order(root: Node) -> Iterable[Subprogram]:
    """Convenience: just the ordered subprograms without the SCC structure."""
    return order_by_dependencies(root).order


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_text(result: OrderingResult) -> str:
    """Plain-text listing: each subprogram with its callees and location."""
    lines: list[str] = []
    for sub in result.order:
        loc = ""
        if sub.source and sub.source.file and sub.source.line:
            loc = f"  {sub.source.file}:{sub.source.line}"
        parent = f"  [in {sub.parent}]" if sub.parent else ""
        lines.append(f"{sub.kind:11s} {sub.display_name}{parent}{loc}")
        for callee in sub.calls:
            target = result.by_name.get(callee)
            shown = target.display_name if target else callee
            lines.append(f"    -> {shown}")
        for ext in sub.external_calls:
            lines.append(f"    -> {ext}  (external)")
    if result.cycles:
        lines.append("")
        lines.append(f"# {len(result.cycles)} cycle(s) detected:")
        for cyc in result.cycles:
            lines.append("  cycle: " + " <-> ".join(s.display_name for s in cyc))
    return "\n".join(lines)


def render_dot(result: OrderingResult) -> str:
    """Render the call graph as a Graphviz dot document."""
    lines = ["digraph callgraph {", '  rankdir="LR";']
    cycle_members: set[str] = {s.name for cyc in result.cycles for s in cyc}
    for sub in result.order:
        shape = {
            "main": "doubleoctagon",
            "function": "ellipse",
            "subroutine": "box",
        }.get(sub.kind, "ellipse")
        attrs = [f'shape={shape}', f'label="{sub.display_name}"']
        if sub.name in cycle_members:
            attrs.append('color="red"')
        lines.append(f'  "{sub.name}" [{", ".join(attrs)}];')
    for sub in result.order:
        for callee in sub.calls:
            lines.append(f'  "{sub.name}" -> "{callee}";')
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers used by Subprogram.source_text
# ---------------------------------------------------------------------------


def _line_span(node: Node) -> tuple[int | None, int | None]:
    """Return (first, last) source line covered by ``node``'s subtree."""
    first: int | None = None
    last: int | None = None
    for n in node.walk():
        if n.source is None or n.source.line is None:
            continue
        s = n.source.line
        e = n.source.end_line or n.source.line
        first = s if first is None else min(first, s)
        last = e if last is None else max(last, e)
    return first, last


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flang-ast-deporder",
        description=(
            "Sort Fortran subprograms by call dependencies "
            "(callees first; mutually-recursive groups stay together)."
        ),
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("source", nargs="?", help="Fortran source file.")
    src.add_argument(
        "--ast",
        type=Path,
        help="Pre-computed AST JSON (from -fdebug-dump-parse-tree-json).",
    )
    p.add_argument("--flang", help="Override flang binary path.")
    p.add_argument(
        "--no-sema",
        action="store_true",
        help="Use the -no-sema flang dump (names are lower-cased original spellings).",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit JSON.")
    fmt.add_argument("--dot", action="store_true", help="Emit Graphviz dot.")
    fmt.add_argument(
        "--text",
        action="store_true",
        help="Emit a human-readable listing (default with no other flag).",
    )
    fmt.add_argument(
        "--names",
        action="store_true",
        help="Emit just the ordered subprogram names, one per line.",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    if args.source:
        root = parse_fortran_file(
            args.source, flang=args.flang, sema=not args.no_sema
        )
    else:
        root = parse_json_file(args.ast)

    result = order_by_dependencies(root)

    if args.json:
        json.dump(result.to_json(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.dot:
        sys.stdout.write(render_dot(result) + "\n")
    elif args.names:
        for sub in result.order:
            sys.stdout.write(sub.display_name + "\n")
    else:
        sys.stdout.write(render_text(result) + "\n")

    if result.cycles:
        # Non-zero exit so scripts can detect cycles; output is still emitted.
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
