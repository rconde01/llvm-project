"""Typed data structures for flang parse tree nodes.

The flang parse tree has hundreds of node kinds.  Rather than declare a
dataclass per kind (which would require keeping a 1000+ entry table in
sync with parse-tree.h), we model every node with a single ``Node`` class
whose ``kind`` field carries the node type as a string.  The ``NodeKind``
enum provides typed constants for the kinds you reach for most often.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Iterator, Sequence


@dataclass(frozen=True, slots=True)
class SourceRange:
    """A half-open source range, as emitted by the JSON dumper."""

    text: str
    """The original source text covered by this node."""

    file: str | None = None
    """Absolute path of the source file, or ``None`` if unknown."""

    line: int | None = None
    """1-based start line."""

    col: int | None = None
    """1-based start column."""

    end_line: int | None = None
    """1-based end line (inclusive of the last line, exclusive of ``end_col``)."""

    end_col: int | None = None
    """1-based end column, exclusive."""

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> SourceRange:
        return cls(
            text=str(raw.get("text", "")),
            file=_opt_str(raw.get("file")),
            line=_opt_int(raw.get("line")),
            col=_opt_int(raw.get("col")),
            end_line=_opt_int(raw.get("endLine")),
            end_col=_opt_int(raw.get("endCol")),
        )

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"text": self.text}
        if self.file is not None:
            out["file"] = self.file
        if self.line is not None:
            out["line"] = self.line
        if self.col is not None:
            out["col"] = self.col
        if self.end_line is not None:
            out["endLine"] = self.end_line
        if self.end_col is not None:
            out["endCol"] = self.end_col
        return out

    def __str__(self) -> str:
        if self.file and self.line is not None and self.col is not None:
            return f"{self.file}:{self.line}:{self.col}"
        return repr(self.text)


@dataclass(frozen=True, slots=True)
class Comment:
    """A comment extracted from the original Fortran source.

    Comments are not part of the parse tree — flang's parser discards
    them — so they are recovered by re-scanning the source file and
    associated with parse tree nodes by the ``annotate`` module.
    """

    text: str
    """Comment body without the leading ``!`` or fixed-form prefix character."""

    raw: str
    """Full comment text including the leading marker."""

    file: str
    """Source file the comment came from."""

    line: int
    """1-based line number containing the comment."""

    col: int
    """1-based column where the comment marker starts."""

    is_full_line: bool
    """True when the comment is the only non-whitespace on its line."""

    is_directive: bool = False
    """True for compiler / OpenMP / OpenACC directives (``!$OMP``, ``!DIR$``, …).

    Directives parse as comments to a free-form Fortran scanner but are
    semantically meaningful, so consumers usually want to handle them
    separately from prose comments.
    """

    def to_json(self) -> dict[str, object]:
        return {
            "text": self.text,
            "raw": self.raw,
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "isFullLine": self.is_full_line,
            "isDirective": self.is_directive,
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> Comment:
        line_raw = raw["line"]
        col_raw = raw["col"]
        if not isinstance(line_raw, int) or not isinstance(col_raw, int):
            raise ValueError("Comment 'line' and 'col' must be integers")
        return cls(
            text=str(raw["text"]),
            raw=str(raw["raw"]),
            file=str(raw["file"]),
            line=line_raw,
            col=col_raw,
            is_full_line=bool(raw.get("isFullLine", False)),
            is_directive=bool(raw.get("isDirective", False)),
        )


@dataclass(slots=True)
class Node:
    """A single parse tree node.

    ``children`` holds the node's structural sub-tree exactly as the JSON
    dumper produced it (transparent wrappers like ``std::variant`` and
    ``std::tuple`` are already elided on the C++ side).
    """

    kind: str
    """The C++ parse tree class name, e.g. ``"AssignmentStmt"`` or ``"Name"``."""

    source: SourceRange | None = None
    """Source range, when the underlying C++ node has a ``source`` member."""

    fortran: str | None = None
    """A Fortran-source rendering of the analyzed value, when available.

    This is set for nodes that carry semantic information back into the
    parse tree (typed expressions, assignments, calls), as well as for
    literal constants and bare identifiers.
    """

    label: int | None = None
    """Statement label, only set on ``Statement`` wrapper nodes."""

    children: list[Node] = field(default_factory=list)
    """Direct sub-nodes, in source order."""

    leading_comments: list[Comment] = field(default_factory=list)
    """Comments attached to this node by ``annotate.annotate_tree``.

    Populated for "anchorable" nodes (statements, program units, …).
    Defaults to empty; ``annotate_tree`` is responsible for filling them.
    """

    trailing_comments: list[Comment] = field(default_factory=list)
    """Inline / same-line comments associated with this node."""

    # -- Construction -------------------------------------------------------

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> Node:
        kind = raw.get("kind")
        if not isinstance(kind, str):
            raise ValueError(f"node is missing string 'kind': {raw!r}")
        source_raw = raw.get("source")
        source: SourceRange | None = None
        if isinstance(source_raw, dict):
            source = SourceRange.from_json(source_raw)
        children_raw = raw.get("children", [])
        if not isinstance(children_raw, list):
            raise ValueError(
                f"node {kind!r}: 'children' must be a list, got {type(children_raw).__name__}"
            )
        children = [cls.from_json(c) for c in children_raw if isinstance(c, dict)]
        leading_raw = raw.get("leadingComments", [])
        trailing_raw = raw.get("trailingComments", [])
        leading = (
            [Comment.from_json(c) for c in leading_raw if isinstance(c, dict)]
            if isinstance(leading_raw, list)
            else []
        )
        trailing = (
            [Comment.from_json(c) for c in trailing_raw if isinstance(c, dict)]
            if isinstance(trailing_raw, list)
            else []
        )
        return cls(
            kind=kind,
            source=source,
            fortran=_opt_str(raw.get("fortran")),
            label=_opt_int(raw.get("label")),
            children=children,
            leading_comments=leading,
            trailing_comments=trailing,
        )

    def to_json(self) -> dict[str, object]:
        """Serialize back to a JSON-compatible dict (round-trip friendly)."""
        out: dict[str, object] = {"kind": self.kind}
        if self.source is not None:
            out["source"] = self.source.to_json()
        if self.fortran is not None:
            out["fortran"] = self.fortran
        if self.label is not None:
            out["label"] = self.label
        if self.leading_comments:
            out["leadingComments"] = [c.to_json() for c in self.leading_comments]
        if self.trailing_comments:
            out["trailingComments"] = [c.to_json() for c in self.trailing_comments]
        if self.children:
            out["children"] = [c.to_json() for c in self.children]
        return out

    # -- Convenience accessors ---------------------------------------------

    def __iter__(self) -> Iterator[Node]:
        """Iterate over direct children."""
        return iter(self.children)

    # NB: we deliberately do **not** define ``__len__`` or ``__bool__``.
    # A Node is conceptually a single AST node, not a container; in
    # particular a leaf node (children == []) must remain *truthy* so
    # idioms like ``a or b`` and ``if maybe_node:`` work as expected.
    # Use ``len(node.children)`` for the child count.

    def __getitem__(self, index: int) -> Node:
        return self.children[index]

    def has_kind(self, *kinds: str | NodeKind) -> bool:
        """Return True if this node's kind matches any of the given names."""
        return self.kind in {str(k) for k in kinds}

    def children_of_kind(self, *kinds: str | NodeKind) -> list[Node]:
        """Return the direct children whose kind matches any of ``kinds``."""
        wanted = {str(k) for k in kinds}
        return [c for c in self.children if c.kind in wanted]

    def first_child(self, *kinds: str | NodeKind) -> Node | None:
        """Return the first direct child of one of the given kinds, or None."""
        wanted = {str(k) for k in kinds}
        for c in self.children:
            if c.kind in wanted:
                return c
        return None

    def require_child(self, *kinds: str | NodeKind) -> Node:
        """Like ``first_child`` but raises if no matching child exists."""
        child = self.first_child(*kinds)
        if child is None:
            wanted = ", ".join(str(k) for k in kinds)
            raise LookupError(
                f"{self.kind!r} has no direct child of kind {{{wanted}}}"
            )
        return child

    # -- Traversal ----------------------------------------------------------

    def walk(self) -> Iterator[Node]:
        """Yield this node, then every descendant (pre-order)."""
        yield self
        for child in self.children:
            yield from child.walk()

    def find_all(self, *kinds: str | NodeKind) -> Iterator[Node]:
        """Yield every descendant (including self) of any of the given kinds."""
        wanted = {str(k) for k in kinds}
        for node in self.walk():
            if node.kind in wanted:
                yield node

    def find_first(self, *kinds: str | NodeKind) -> Node | None:
        """Return the first descendant (or self) matching any of ``kinds``."""
        return next(self.find_all(*kinds), None)

    def count(self, *kinds: str | NodeKind) -> int:
        """Return the number of descendants (including self) matching ``kinds``."""
        return sum(1 for _ in self.find_all(*kinds))

    # -- Rendering ----------------------------------------------------------

    def __repr__(self) -> str:
        bits: list[str] = [f"kind={self.kind!r}"]
        if self.fortran is not None:
            bits.append(f"fortran={self.fortran!r}")
        if self.source is not None and self.source.line is not None:
            bits.append(f"@{self.source.line}:{self.source.col}")
        bits.append(f"children=[{len(self.children)}]")
        return f"Node({', '.join(bits)})"

    def pretty(self, *, indent: int = 0, max_depth: int | None = None) -> str:
        """Return an indented multi-line string representation."""
        lines: list[str] = []
        self._render(lines, depth=0, indent=indent, max_depth=max_depth)
        return "\n".join(lines)

    def _render(
        self,
        lines: list[str],
        *,
        depth: int,
        indent: int,
        max_depth: int | None,
    ) -> None:
        prefix = " " * (indent + depth * 2)
        head = f"{prefix}{self.kind}"
        if self.fortran is not None:
            head += f" = {self.fortran!r}"
        if self.source is not None and self.source.line is not None:
            head += f"  [{self.source.line}:{self.source.col}-{self.source.end_line}:{self.source.end_col}]"
        lines.append(head)
        if max_depth is not None and depth >= max_depth:
            if self.children:
                lines.append(f"{prefix}  ... ({len(self.children)} children)")
            return
        for child in self.children:
            child._render(
                lines, depth=depth + 1, indent=indent, max_depth=max_depth
            )


def _opt_str(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _opt_int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


# ---------------------------------------------------------------------------
# A small, opinionated enum of the parse tree node kinds most useful for
# tooling that walks the tree (e.g. a Fortran-to-C++ converter).  This is
# intentionally not exhaustive; use the raw ``kind`` string for anything
# not listed here.  Values match the C++ node class names exactly.
# ---------------------------------------------------------------------------


class NodeKind(StrEnum):
    # Top-level structure
    Program = "Program"
    ProgramUnit = "ProgramUnit"
    MainProgram = "MainProgram"
    Module = "Module"
    Submodule = "Submodule"
    BlockData = "BlockData"
    FunctionSubprogram = "FunctionSubprogram"
    SubroutineSubprogram = "SubroutineSubprogram"

    # Statement wrappers (added by the JSON dumper)
    Statement = "Statement"
    UnlabeledStatement = "UnlabeledStatement"

    # Program / module / subprogram statements
    ProgramStmt = "ProgramStmt"
    EndProgramStmt = "EndProgramStmt"
    ModuleStmt = "ModuleStmt"
    EndModuleStmt = "EndModuleStmt"
    FunctionStmt = "FunctionStmt"
    EndFunctionStmt = "EndFunctionStmt"
    SubroutineStmt = "SubroutineStmt"
    EndSubroutineStmt = "EndSubroutineStmt"
    UseStmt = "UseStmt"
    ImportStmt = "ImportStmt"
    ImplicitStmt = "ImplicitStmt"
    ContainsStmt = "ContainsStmt"

    # Declarations
    SpecificationPart = "SpecificationPart"
    DeclarationConstruct = "DeclarationConstruct"
    SpecificationConstruct = "SpecificationConstruct"
    TypeDeclarationStmt = "TypeDeclarationStmt"
    DeclarationTypeSpec = "DeclarationTypeSpec"
    IntrinsicTypeSpec = "IntrinsicTypeSpec"
    IntegerTypeSpec = "IntegerTypeSpec"
    DerivedTypeDef = "DerivedTypeDef"
    DerivedTypeStmt = "DerivedTypeStmt"
    DerivedTypeSpec = "DerivedTypeSpec"
    EntityDecl = "EntityDecl"
    AttrSpec = "AttrSpec"
    ParameterStmt = "ParameterStmt"

    # Execution
    ExecutionPart = "ExecutionPart"
    ExecutionPartConstruct = "ExecutionPartConstruct"
    ExecutableConstruct = "ExecutableConstruct"
    Block = "Block"
    ActionStmt = "ActionStmt"

    # Common statements
    AssignmentStmt = "AssignmentStmt"
    PointerAssignmentStmt = "PointerAssignmentStmt"
    CallStmt = "CallStmt"
    IfStmt = "IfStmt"
    IfConstruct = "IfConstruct"
    IfThenStmt = "IfThenStmt"
    ElseIfStmt = "ElseIfStmt"
    ElseStmt = "ElseStmt"
    EndIfStmt = "EndIfStmt"
    DoConstruct = "DoConstruct"
    NonLabelDoStmt = "NonLabelDoStmt"
    LabelDoStmt = "LabelDoStmt"
    EndDoStmt = "EndDoStmt"
    LoopControl = "LoopControl"
    SelectCaseStmt = "SelectCaseStmt"
    CaseConstruct = "CaseConstruct"
    CaseStmt = "CaseStmt"
    ReturnStmt = "ReturnStmt"
    StopStmt = "StopStmt"
    ContinueStmt = "ContinueStmt"
    CycleStmt = "CycleStmt"
    ExitStmt = "ExitStmt"
    PrintStmt = "PrintStmt"
    ReadStmt = "ReadStmt"
    WriteStmt = "WriteStmt"
    AllocateStmt = "AllocateStmt"
    DeallocateStmt = "DeallocateStmt"

    # Expressions / leaves
    Expr = "Expr"
    Variable = "Variable"
    Designator = "Designator"
    DataRef = "DataRef"
    Name = "Name"
    LiteralConstant = "LiteralConstant"
    IntLiteralConstant = "IntLiteralConstant"
    RealLiteralConstant = "RealLiteralConstant"
    CharLiteralConstant = "CharLiteralConstant"
    LogicalLiteralConstant = "LogicalLiteralConstant"
    FunctionReference = "FunctionReference"

    def __str__(self) -> str:  # noqa: D401 - StrEnum returns name by default
        return self.value


# Re-exports useful when consumers want to type-annotate tree slices.
NodeList = Sequence[Node]
NodeIterable = Iterable[Node]
