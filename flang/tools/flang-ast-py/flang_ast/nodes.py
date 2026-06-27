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

    sym_type: str | None = None
    """Resolved declared type as Fortran text (e.g. ``"REAL(8)"``,
    ``"INTEGER(4)"``, ``"TYPE(point)"``).  Set by the dumper after
    semantics: for a ``Name`` it is the symbol's declared type; for an
    expression-bearing node (``Expr``, ``Variable``, ``DataStmtConstant``,
    ``AllocateObject``, ``PointerObject``) it is the type the expression
    evaluates to.  ``None`` when no resolved type is available."""

    rank: int | None = None
    """Rank of this node's resolved entity (0 for scalars).  For a
    ``Name`` it is the symbol's rank; for an expression-bearing node it is
    the expression's rank (scalar/array/section)."""

    category: str | None = None
    """For an expression-bearing node, one of:

    * ``"variable"`` — an assignable designator (a name, array element,
      array section, substring, structure component) with storage.
    * ``"constant"`` — an expression that folds to a known compile-time
      value (literals, ``PARAMETER`` references, constant arithmetic).
    * ``"expression"`` — anything else: arithmetic results, function-call
      results, operations on variables — a value, not a designator.

    ``None`` when the node carries no analyzed expression."""

    value: str | None = None
    """For a scalar integer constant expression, the folded value as a
    decimal string (e.g. ``"25"`` for ``MAXSIZ`` after parameter
    substitution).  ``None`` when the expression is not a scalar integer
    constant or could not be evaluated."""

    lower_present: bool | None = None
    """For a ``SubstringRange`` (``s(lo:hi)``), whether the lower bound was
    written.  Both bounds are optional and an omitted one is absent from the
    children, so a lone present bound is positionally ambiguous; these flags
    disambiguate ``s(:hi)`` (lower absent) from ``s(lo:)`` (upper absent).
    ``None`` for any other node."""

    upper_present: bool | None = None
    """For a ``SubstringRange`` or ``SubscriptTriplet``, whether the upper
    bound was written.  See :attr:`lower_present`."""

    stride_present: bool | None = None
    """For a ``SubscriptTriplet`` (``lo:hi:stride``), whether the stride was
    written.  Disambiguates which collapsed optional a lone bound is.
    ``None`` for any other node."""

    is_object: bool = False
    """True when a ``Name`` resolves to an object entity (a variable)."""

    is_proc: bool = False
    """True when a ``Name`` resolves to a procedure (subprogram, dummy
    procedure, external, or intrinsic) — i.e. ``name(...)`` is a call."""

    assoc: str | None = None
    """``"use"`` / ``"host"`` when a ``Name`` is module/host-associated
    state rather than a local of the enclosing unit; ``None`` otherwise."""

    attrs: tuple[str, ...] = ()
    """Resolved symbol attributes on this ``Name``, lowercased — e.g.
    ``("intent_in",)``, ``("optional", "save")``, ``("external",)``.  Set
    from ``Symbol::attrs()`` after semantics, so a standalone
    ``INTENT(IN) :: x`` decorates ``x`` the same way ``REAL, INTENT(IN) ::
    x`` does, and the converter need not walk parse-tree ``AttrSpec`` to
    rediscover them."""

    shape: list[tuple[int, int]] | None = None
    """Explicit, constant array shape from the resolved symbol: one
    ``(lower, upper)`` pair per dimension.  ``None`` for scalars or when a
    bound isn't a compile-time constant."""

    common_block: str | None = None
    """Name of the COMMON block this Object belongs to (``"blk"`` for
    ``common /blk/ x``).  ``None`` for symbols not in any COMMON.  Set by
    the dumper from the resolved symbol's ``ObjectEntityDetails::commonBlock()``,
    so the converter does not need to walk ``CommonStmt`` parse-tree
    nodes to discover membership."""

    equivalence_class: int | None = None
    """0-based index within the owning scope's equivalence-set list, for
    objects that participate in an ``EQUIVALENCE`` statement.  All
    co-aliased symbols share the same index.  ``None`` for symbols not
    in any EQUIVALENCE."""

    defined_in: str | None = None
    """Owning derived-type name when the resolved symbol is a type
    component (``"pt"`` on ``x`` for ``type :: pt; real :: x; end type``).
    ``None`` when the symbol is not a component."""

    proc_interface: str | None = None
    """For a procedure entity declared ``procedure(iface), …``, the
    resolved interface symbol's name.  ``None`` when there is no
    explicit interface."""

    is_implicit: bool = False
    """True when a ``Name``'s symbol was typed by implicit-typing rules
    rather than an explicit declaration."""

    defined_at: SourceRange | None = None
    """Declaring source location for a Name's use site.  Same sub-object
    shape as ``source``.  Omitted on the declaring Name itself."""

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
        defined_at_raw = raw.get("defined_at")
        defined_at = (
            SourceRange.from_json(defined_at_raw)
            if isinstance(defined_at_raw, dict)
            else None
        )
        return cls(
            kind=kind,
            source=source,
            fortran=_opt_str(raw.get("fortran")),
            label=_opt_int(raw.get("label")),
            sym_type=_opt_str(raw.get("type")),
            rank=_opt_int(raw.get("rank")),
            is_object=raw.get("object") is True,
            is_proc=raw.get("proc") is True,
            assoc=_opt_str(raw.get("assoc")),
            attrs=_opt_str_tuple(raw.get("attrs")),
            category=_opt_str(raw.get("category")),
            value=_opt_str(raw.get("value")),
            lower_present=(
                bool(raw["lowerPresent"]) if "lowerPresent" in raw else None
            ),
            upper_present=(
                bool(raw["upperPresent"]) if "upperPresent" in raw else None
            ),
            stride_present=(
                bool(raw["stridePresent"]) if "stridePresent" in raw else None
            ),
            shape=_opt_shape(raw.get("shape")),
            common_block=_opt_str(raw.get("common_block")),
            equivalence_class=_opt_int(raw.get("equivalence_class")),
            defined_in=_opt_str(raw.get("defined_in")),
            proc_interface=_opt_str(raw.get("proc_interface")),
            is_implicit=raw.get("implicit") is True,
            defined_at=defined_at,
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
        if self.sym_type is not None:
            out["type"] = self.sym_type
        if self.rank is not None:
            out["rank"] = self.rank
        if self.is_object:
            out["object"] = True
        if self.is_proc:
            out["proc"] = True
        if self.assoc is not None:
            out["assoc"] = self.assoc
        if self.attrs:
            out["attrs"] = list(self.attrs)
        if self.category is not None:
            out["category"] = self.category
        if self.value is not None:
            out["value"] = self.value
        if self.shape is not None:
            out["shape"] = [[lo, hi] for lo, hi in self.shape]
        if self.common_block is not None:
            out["common_block"] = self.common_block
        if self.equivalence_class is not None:
            out["equivalence_class"] = self.equivalence_class
        if self.defined_in is not None:
            out["defined_in"] = self.defined_in
        if self.proc_interface is not None:
            out["proc_interface"] = self.proc_interface
        if self.is_implicit:
            out["implicit"] = True
        if self.defined_at is not None:
            out["defined_at"] = self.defined_at.to_json()
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


def _opt_str_tuple(v: object) -> tuple[str, ...]:
    if not isinstance(v, list):
        return ()
    return tuple(s for s in v if isinstance(s, str))


def _opt_shape(v: object) -> list[tuple[int, int]] | None:
    if not isinstance(v, list):
        return None
    out: list[tuple[int, int]] = []
    for dim in v:
        if (
            isinstance(dim, list)
            and len(dim) == 2
            and all(isinstance(b, int) and not isinstance(b, bool) for b in dim)
        ):
            out.append((dim[0], dim[1]))
        else:
            return None
    return out


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
