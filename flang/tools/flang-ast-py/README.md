# flang-ast

A small, typed Python library that loads the JSON parse tree emitted by

```
flang -fc1 -fdebug-dump-analyzed-tree-json[-no-sema] file.f90
```

into Python data structures suitable for building tooling on top of
flang — for example, a Fortran-to-C++ source converter.

## Install (editable, for development)

```bash
cd flang/tools/flang-ast-py
pip install -e .
```

The package has no runtime dependencies; it only needs Python 3.11+.

## Usage

### Drive flang directly

```python
from flang_ast import parse_fortran_file, NodeKind

program = parse_fortran_file("hello.f90")  # sema on by default

for assign in program.find_all(NodeKind.AssignmentStmt):
    print(assign.source, "->", assign.fortran)
```

`parse_fortran_file` looks for `flang-new`/`flang` on `$PATH`, or honors
the `FLANG` environment variable, or accepts an explicit `flang=...`
argument.  Pass `sema=False` to use `-fdebug-dump-analyzed-tree-json-no-sema`.

### Load JSON you already have

```python
from flang_ast import parse_json_file, parse_json_string

root = parse_json_file("dump.json")
root2 = parse_json_string(open("dump.json").read())
```

### The data model

Every parse tree node is a `Node`:

```python
@dataclass(slots=True)
class Node:
    kind: str                   # C++ parse-tree class name
    source: SourceRange | None  # original source range, when present
    fortran: str | None         # analyzed Fortran rendering (e.g. "1_4")
    label: int | None           # only set on Statement wrappers
    children: list[Node]        # direct children, in source order
```

`SourceRange` exposes `text`, `file`, `line`/`col`, and `end_line`/`end_col`
(all 1-based, half-open).

### Navigating the tree

```python
# Pre-order walk
for node in program.walk():
    ...

# Find by kind (descendants, including self)
for name in program.find_all("Name"):
    print(name.fortran, "at", name.source)

# Direct children only
specs = program.children_of_kind(NodeKind.SpecificationConstruct)

# First match (returns None if absent)
main = program.find_first(NodeKind.MainProgram)

# Required first child
prog_stmt = main.require_child("Statement").require_child("ProgramStmt")
```

### Visitor pattern

```python
from flang_ast import Node, NodeVisitor

class CallCollector(NodeVisitor[None]):
    def __init__(self) -> None:
        self.calls: list[Node] = []

    def visit_CallStmt(self, node: Node) -> None:
        self.calls.append(node)
        self.generic_visit(node)

collector = CallCollector()
collector.visit(program)
```

`NodeTransformer` is the same idea but returns a (possibly rewritten)
node from each method — children paths are reused when nothing changes.

### Comment annotation

flang discards source comments during parsing, so they aren't present
in the JSON dump.  `flang_ast.annotate` re-scans the source file and
attaches each comment to the most plausible parse tree node:

  * **Trailing** — same-line inline comments (`x = 1  ! count`).
  * **Leading** — full-line comments immediately above a construct;
    consecutive comment lines are grouped into one block.

```python
from flang_ast import annotate_tree, parse_fortran_file

program = parse_fortran_file("hello.f90")
annotate_tree(program)

for stmt in program.find_all("Statement"):
    for c in stmt.leading_comments:
        print("  before:", c.raw.strip())
    for c in stmt.trailing_comments:
        print("  inline:", c.raw.strip())
```

OpenMP / OpenACC / `!DIR$` directives are recognized and flagged with
`is_directive=True` so you can route them separately.  Fixed-form files
(`.f`, `.for`) are detected automatically; pass `fixed_form=True` to
force it.

### CLI

The package installs a `flang-ast-annotate` console script, and is also
runnable via `python -m flang_ast`:

```bash
# JSON output with leadingComments/trailingComments fields populated
flang-ast-annotate hello.f90

# Human-readable summary
flang-ast-annotate hello.f90 --report

# Re-use a pre-computed AST
flang -fc1 -fdebug-dump-analyzed-tree-json hello.f90 > dump.json
flang-ast-annotate --ast dump.json --source-file hello.f90 --report
```

### Dependency-ordered subprograms

`flang_ast.depgraph` finds every `MainProgram`, `FunctionSubprogram`,
and `SubroutineSubprogram` (including those nested in modules and
`CONTAINS` blocks), builds the call graph, and topologically sorts it
so that **every callee appears before its caller**.  Mutually recursive
groups stay together as a single SCC, sorted by source line internally.

```python
from flang_ast import order_by_dependencies, parse_fortran_file

result = order_by_dependencies(parse_fortran_file("hello.f90"))
for sub in result.order:
    print(sub.kind, sub.display_name, "calls:", sub.calls)

if result.cycles:
    print("warning: mutually-recursive groups present")
    for scc in result.cycles:
        print(" ", " <-> ".join(s.display_name for s in scc))
```

Each `Subprogram` carries its parse tree `node`, source range, list of
in-graph `calls`, `external_calls` (intrinsics or names imported via
`USE` that don't resolve to a local definition), and a `source_text()`
method that returns the original source slice for the unit.

CLI:

```bash
# Human-readable listing (default)
flang-ast-deporder hello.f90

# Just the ordered names, one per line — handy for shell pipelines
flang-ast-deporder hello.f90 --names

# Full structured output
flang-ast-deporder hello.f90 --json

# Graphviz dot
flang-ast-deporder hello.f90 --dot | dot -Tsvg -o callgraph.svg
```

Exit code is `2` when the call graph contains a cycle, so wrapper
scripts can detect mutual recursion without parsing the output.

## Why a single `Node` class instead of one per kind?

The flang parse tree defines ~600 node classes.  Mirroring each one as a
Python dataclass would create a giant maintenance burden and a constant
drift risk against `parse-tree.h`.  Instead this library keeps the data
representation generic, ships a `NodeKind` `StrEnum` covering the kinds
you reach for most often, and uses string-based queries elsewhere.  All
kind names are the exact C++ class names, so anything you see in the
JSON or in `dump-parse-tree.h` works as a query string.
