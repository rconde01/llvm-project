"""Associate source comments with parse tree nodes.

flang's parser discards comments, so the JSON dump never contains them.
This module recovers them by re-scanning the original source file and
attaches each comment to the nearest parse tree node:

  * **Trailing comments**  — comments that sit on the same line as a
    statement, after the code (``x = 1  ! count``).
  * **Leading comments**   — full-line comments immediately preceding a
    statement / construct, with no intervening code.  A run of
    consecutive comment lines is attached as a block to the statement
    that follows them.

The script is also runnable as a CLI:

    python -m flang_ast.annotate hello.f90              # writes JSON
    python -m flang_ast.annotate hello.f90 --report     # human-readable
    python -m flang_ast.annotate --ast dump.json --source hello.f90
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .nodes import Comment, Node, SourceRange
from .parser import parse_json_file
from .runner import parse_fortran_file


# ---------------------------------------------------------------------------
# Comment extraction
# ---------------------------------------------------------------------------


_DIRECTIVE_PREFIXES: tuple[str, ...] = (
    "$",       # !$ (OpenMP conditional compilation), !$omp, !$acc
    "DIR$",    # Intel-style directives
    "dir$",
    "GCC$",    # GCC directives
    "gcc$",
)

_FIXED_FORM_SUFFIXES: frozenset[str] = frozenset({".f", ".for", ".ftn", ".F", ".FOR"})


def _is_fixed_form(path: str | os.PathLike[str]) -> bool:
    suffix = Path(os.fspath(path)).suffix
    return suffix in _FIXED_FORM_SUFFIXES


def _classify_directive(body: str) -> bool:
    stripped = body.lstrip()
    for prefix in _DIRECTIVE_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


def extract_comments(
    source: str, file: str, *, fixed_form: bool = False
) -> list[Comment]:
    """Extract every Fortran comment from ``source``.

    The scanner understands single- and double-quoted strings (including
    doubled-quote escapes) and the Fortran continuation marker (``&``).
    It does **not** strip preprocessor directives or holerith literals;
    if you need those, pre-process the file first.
    """
    out: list[Comment] = []
    for line_no, line in enumerate(source.splitlines(), start=1):
        # Strip trailing newline-equivalents but keep internal whitespace.
        comment = _find_comment_on_line(line, fixed_form=fixed_form)
        if comment is None:
            continue
        col, marker_len = comment
        marker = line[col : col + marker_len]
        body = line[col + marker_len :]
        # ``is_full_line`` is true when everything before the comment marker
        # is whitespace.
        is_full_line = not line[:col].strip()
        out.append(
            Comment(
                text=body,
                raw=marker + body,
                file=file,
                line=line_no,
                col=col + 1,  # convert to 1-based
                is_full_line=is_full_line,
                is_directive=_classify_directive(body),
            )
        )
    return out


def _find_comment_on_line(
    line: str, *, fixed_form: bool
) -> tuple[int, int] | None:
    """Return ``(zero_based_col, marker_len)`` of the comment on this line, if any."""
    # Fixed-form: a comment occupies the whole line if column 1 holds
    # one of C, c, *, !, or d/D (the D-line debug convention).
    if fixed_form and line:
        first = line[0]
        if first in ("C", "c", "*", "!", "d", "D"):
            return 0, 1
        # Fixed-form code columns are 7-72; anything in 1-5 is a label or
        # blank.  For inline "!" we fall through to the free-form scanner
        # below; this is the modern Fortran extension fixed-form allows.

    i = 0
    n = len(line)
    in_squote = False
    in_dquote = False
    while i < n:
        ch = line[i]
        if in_squote:
            if ch == "'":
                # Doubled apostrophe is an escape.
                if i + 1 < n and line[i + 1] == "'":
                    i += 2
                    continue
                in_squote = False
        elif in_dquote:
            if ch == '"':
                if i + 1 < n and line[i + 1] == '"':
                    i += 2
                    continue
                in_dquote = False
        else:
            if ch == "'":
                in_squote = True
            elif ch == '"':
                in_dquote = True
            elif ch == "!":
                return i, 1
        i += 1
    return None


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------


# Node kinds that we attach comments to.  Anything else carries source
# ranges through to its children but does not "anchor" comment blocks.
_ANCHOR_KINDS: frozenset[str] = frozenset(
    {
        # Whole-program containers
        "Program",
        "ProgramUnit",
        "MainProgram",
        "Module",
        "Submodule",
        "BlockData",
        "FunctionSubprogram",
        "SubroutineSubprogram",
        "InternalSubprogram",
        "ModuleSubprogram",
        "SeparateModuleSubprogram",
        # Statement wrappers
        "Statement",
        "UnlabeledStatement",
        # Major constructs
        "IfConstruct",
        "DoConstruct",
        "CaseConstruct",
        "SelectCaseStmt",
        "SelectTypeConstruct",
        "SelectRankConstruct",
        "AssociateConstruct",
        "BlockConstruct",
        "WhereConstruct",
        "ForallConstruct",
        "DerivedTypeDef",
        # Specification anchors
        "SpecificationConstruct",
        "DeclarationConstruct",
        "ExecutionPartConstruct",
        "ExecutableConstruct",
        "ImplicitPart",
        "SpecificationPart",
        "ExecutionPart",
    }
)


@dataclass(slots=True)
class _AnchorInfo:
    node: Node
    file: str
    start_line: int
    end_line: int


def _collect_anchors(
    node: Node, anchors: list[_AnchorInfo]
) -> tuple[str | None, int | None, int | None]:
    """Walk the tree, recording every anchor node with a source range.

    Returns the (file, start_line, end_line) span of ``node``, computed
    from the union of its own source and its children's spans.  This
    lets us anchor constructs that don't carry a source range directly
    (e.g. ``IfConstruct``) using the union of their statements.
    """
    file_: str | None = None
    start: int | None = None
    end: int | None = None
    if node.source is not None and node.source.line is not None:
        file_ = node.source.file
        start = node.source.line
        end = node.source.end_line or node.source.line

    for child in node.children:
        c_file, c_start, c_end = _collect_anchors(child, anchors)
        if c_file is None or c_start is None or c_end is None:
            continue
        if file_ is None:
            file_, start, end = c_file, c_start, c_end
        elif c_file == file_:
            assert start is not None and end is not None
            start = min(start, c_start)
            end = max(end, c_end)

    if (
        node.kind in _ANCHOR_KINDS
        and file_ is not None
        and start is not None
        and end is not None
    ):
        anchors.append(_AnchorInfo(node, file_, start, end))

    return file_, start, end


class CommentAnnotator:
    """Attach source comments to parse tree nodes.

    Construct one annotator per project / run.  Source files are loaded
    lazily from disk on demand; provide pre-loaded sources via the
    ``sources`` dict if you want to avoid filesystem access (e.g. when
    annotating an in-memory snippet).
    """

    def __init__(
        self,
        sources: dict[str, str] | None = None,
        *,
        fixed_form_override: bool | None = None,
    ) -> None:
        self._sources: dict[str, str] = dict(sources or {})
        self._fixed_form_override = fixed_form_override
        self._comments_cache: dict[str, list[Comment]] = {}

    # -- Public API --------------------------------------------------------

    def annotate(self, root: Node) -> Node:
        """Attach comments to ``root`` and return it.

        Modifies the node tree in place (re-using existing ``Node``
        instances) and also returns it for convenient chaining.
        """
        anchors: list[_AnchorInfo] = []
        _collect_anchors(root, anchors)
        # Group anchors by file so we can match against the comments from
        # the same source.
        anchors_by_file: dict[str, list[_AnchorInfo]] = {}
        for a in anchors:
            anchors_by_file.setdefault(a.file, []).append(a)

        for file_, file_anchors in anchors_by_file.items():
            comments = self._comments_for(file_)
            if not comments:
                continue
            self._attach(file_anchors, comments)
        return root

    def comments_for_file(self, file: str) -> list[Comment]:
        """Return the cached / freshly-extracted comments for ``file``."""
        return list(self._comments_for(file))

    # -- Internals ---------------------------------------------------------

    def _comments_for(self, file: str) -> list[Comment]:
        cached = self._comments_cache.get(file)
        if cached is not None:
            return cached
        source = self._sources.get(file)
        if source is None:
            try:
                source = Path(file).read_text(encoding="utf-8", errors="replace")
            except OSError:
                source = ""
            self._sources[file] = source
        fixed = (
            self._fixed_form_override
            if self._fixed_form_override is not None
            else _is_fixed_form(file)
        )
        result = extract_comments(source, file, fixed_form=fixed)
        self._comments_cache[file] = result
        return result

    @staticmethod
    def _attach(anchors: list[_AnchorInfo], comments: list[Comment]) -> None:
        # Sort anchors by their starting line so leading-comment search
        # can walk linearly.
        anchors_sorted = sorted(anchors, key=lambda a: (a.start_line, a.end_line))

        # ---- Trailing comments ---------------------------------------
        # An inline comment lands on a single line; attach it to the most
        # specific anchor (smallest enclosing span) that covers that line.
        for c in comments:
            if c.is_full_line:
                continue
            best: _AnchorInfo | None = None
            best_span: int = -1
            for a in anchors_sorted:
                if a.start_line <= c.line <= a.end_line:
                    span = a.end_line - a.start_line
                    # Prefer narrower (more specific) anchors.
                    if best is None or span < best_span:
                        best, best_span = a, span
            if best is not None:
                best.node.trailing_comments.append(c)

        # ---- Leading comment blocks ----------------------------------
        # A run of consecutive full-line comments above a code line is
        # the leading-comment block of the first statement after them.
        # We compute the set of comment lines, then scan anchors in order
        # and pull in any contiguous block immediately above them.
        full_line_comments: dict[int, Comment] = {
            c.line: c for c in comments if c.is_full_line
        }
        if not full_line_comments:
            return

        # Pre-compute, for each anchor, its top-level "outer" anchor so
        # that a block of leading comments is only attached once (to the
        # outermost anchor that starts on that line).
        assigned_lines: set[int] = set()
        for a in anchors_sorted:
            block: list[Comment] = []
            probe = a.start_line - 1
            while probe in full_line_comments and probe not in assigned_lines:
                block.append(full_line_comments[probe])
                probe -= 1
            if not block:
                continue
            block.reverse()
            # Filter: don't re-attach comments already assigned to a more
            # specific (later) anchor.
            fresh = [c for c in block if c.line not in assigned_lines]
            if not fresh:
                continue
            a.node.leading_comments.extend(fresh)
            assigned_lines.update(c.line for c in fresh)


def annotate_tree(
    root: Node,
    *,
    sources: dict[str, str] | None = None,
    fixed_form: bool | None = None,
) -> Node:
    """Convenience wrapper: build an annotator and run it once."""
    return CommentAnnotator(sources=sources, fixed_form_override=fixed_form).annotate(root)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(root: Node) -> str:
    """Render a human-readable summary of every node carrying comments."""
    lines: list[str] = []
    for node in root.walk():
        if not node.leading_comments and not node.trailing_comments:
            continue
        loc = _format_loc(node.source)
        lines.append(f"{node.kind}  {loc}")
        for c in node.leading_comments:
            lines.append(f"    [leading  {c.line}:{c.col}] {c.raw.rstrip()}")
        for c in node.trailing_comments:
            lines.append(f"    [trailing {c.line}:{c.col}] {c.raw.rstrip()}")
    if not lines:
        return "(no comments associated)"
    return "\n".join(lines)


def _format_loc(source: SourceRange | None) -> str:
    if source is None or source.line is None:
        return ""
    if source.file:
        return f"({source.file}:{source.line}:{source.col})"
    return f"(line {source.line}:{source.col})"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flang-ast-annotate",
        description="Annotate flang's parse tree with comments from the source.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "source",
        nargs="?",
        help="Fortran source file (.f90 etc.).  flang is invoked to obtain the AST.",
    )
    src.add_argument(
        "--ast",
        type=Path,
        help="Pre-computed AST JSON file (produced by -fdebug-dump-analyzed-tree-json).",
    )
    p.add_argument(
        "--source-file",
        type=Path,
        help="Fortran source file to scan for comments (required with --ast "
        "if the AST's recorded path is not accessible).",
    )
    p.add_argument(
        "--no-sema",
        action="store_true",
        help="Use -fdebug-dump-analyzed-tree-json-no-sema when invoking flang.",
    )
    p.add_argument(
        "--flang",
        help="Path to the flang binary (overrides $FLANG / $PATH lookup).",
    )
    p.add_argument(
        "--fixed-form",
        action="store_true",
        help="Treat sources as fixed-form regardless of extension.",
    )
    p.add_argument(
        "--report",
        action="store_true",
        help="Emit a human-readable report instead of JSON.",
    )
    p.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent width (set to 0 for compact output).",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    if args.source:
        root = parse_fortran_file(
            args.source,
            flang=args.flang,
            sema=not args.no_sema,
        )
        sources: dict[str, str] | None = None
        if args.source_file:
            sources = {
                str(args.source_file.resolve()): args.source_file.read_text(
                    encoding="utf-8", errors="replace"
                )
            }
    else:
        root = parse_json_file(args.ast)
        sources = None
        if args.source_file:
            sources = {
                str(args.source_file.resolve()): args.source_file.read_text(
                    encoding="utf-8", errors="replace"
                )
            }

    fixed = True if args.fixed_form else None
    annotate_tree(root, sources=sources, fixed_form=fixed)

    if args.report:
        print(render_report(root))
        return 0

    indent = args.indent if args.indent > 0 else None
    json.dump(root.to_json(), sys.stdout, indent=indent)
    sys.stdout.write("\n")
    return 0


def _iter_all_comments(roots: Iterable[Node]) -> Iterable[Comment]:
    """Convenience: yield every comment attached anywhere under ``roots``."""
    for root in roots:
        for node in root.walk():
            yield from node.leading_comments
            yield from node.trailing_comments


if __name__ == "__main__":  # pragma: no cover - exercised via tests
    sys.exit(main())
