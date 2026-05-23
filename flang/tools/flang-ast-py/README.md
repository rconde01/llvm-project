# flang-ast

A small, typed Python library that loads the JSON parse tree emitted by

```
flang -fc1 -fdebug-dump-parse-tree-json[-no-sema] file.f90
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
argument.  Pass `sema=False` to use `-fdebug-dump-parse-tree-json-no-sema`.

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

## Why a single `Node` class instead of one per kind?

The flang parse tree defines ~600 node classes.  Mirroring each one as a
Python dataclass would create a giant maintenance burden and a constant
drift risk against `parse-tree.h`.  Instead this library keeps the data
representation generic, ships a `NodeKind` `StrEnum` covering the kinds
you reach for most often, and uses string-based queries elsewhere.  All
kind names are the exact C++ class names, so anything you see in the
JSON or in `dump-parse-tree.h` works as a query string.
