<!--===- docs/AnalyzedTreeJSONDumper.md
  
   Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
   See https://llvm.org/LICENSE.txt for license information.
   SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
  
-->

# Analyzed-tree JSON dumper

```{contents}
---
local:
---
```

## Purpose

`-fdebug-dump-analyzed-tree-json` writes the parse tree to standard output
as a single-line JSON document.  Unlike `-fdebug-dump-parse-tree`
(structure only) or `-fdebug-dump-symbols` (symbols only), it bundles the
parse-tree structure with the resolved-symbol facts and analyzed-expression
facts that semantics already computed.  A downstream tool — source
rewriter, analyzer, IDE service — reads both in one pass without
re-parsing free-form Fortran or re-running semantics.

The companion `-fdebug-dump-analyzed-tree-json-no-sema` action emits the
same structural shape but skips semantic analysis (parse-tree only — no
`type`, `category`, `attrs`, `assoc`, `object`, `proc`, `shape`).  Useful
when a consumer needs to read source that semantics would reject.

## Top-level shape

The output is exactly one JSON object.  Its root `kind` is `"Program"`,
and every parse-tree node it visits becomes a child JSON object.  No
trailing newline.

```
{"kind":"Program","children":[ {"kind":"ProgramUnit","children":[ ... ]} ]}
```

The dumper emits all fields on one line; pipe through `python -m
json.tool` or `jq` to pretty-print.

## Per-node fields

Every emitted JSON object has at least a `kind` field.  All others are
optional and appear only when the underlying parse-tree node carries the
information.

| Field      | Type             | Description                                                  |
| ---------- | ---------------- | ------------------------------------------------------------ |
| `kind`     | string           | Parse-tree node class name (matches `-fdebug-dump-parse-tree`'s label). |
| `source`   | object           | Source range — only when the node carries a `source` member. See [Source ranges](#source-ranges). |
| `fortran`  | string           | Analyzed Fortran source rendering — only when the node carries an analyzed expression or a resolved name that semantics can re-render. |
| `label`    | integer          | Statement label, on `Statement` nodes only when the source had one. |
| `children` | array of objects | Direct child parse-tree nodes — omitted when the node has no children. |

`Name` nodes whose symbol is resolved add:

| Field    | Type            | Description                                                  |
| -------- | --------------- | ------------------------------------------------------------ |
| `type`   | string          | Resolved type (e.g. `"INTEGER(4)"`, `"REAL(8)"`, `"CHARACTER(LEN=8,KIND=1)"`). |
| `rank`   | integer         | Resolved rank.  `0` for scalars; `1..7` for arrays.          |
| `shape`  | array of pairs  | `[[lo,hi],…]` per dimension — only when every bound is a constant-foldable integer (Fortran-deferred shapes, assumed shapes, and run-time-bound arrays omit this). |
| `object` | boolean (`true`) | Symbol classifies as an object entity (a variable / parameter / dummy data). |
| `proc`   | boolean (`true`) | Symbol classifies as a procedure (subroutine, function, external, or intrinsic). |
| `attrs`  | array of strings | The resolved-symbol attribute set, lowercased.  See [Attributes](#attributes). |
| `assoc`  | string          | `"use"` if module-use-associated, `"host"` if host-associated. Omitted for local symbols. |

Nodes whose analyzed `typedExpr` is populated (`Expr`, `Variable`,
`DataStmtConstant`, `AllocateObject`, `PointerObject`) add:

| Field      | Type    | Description                                                  |
| ---------- | ------- | ------------------------------------------------------------ |
| `type`     | string  | Resolved expression type.                                    |
| `rank`     | integer | Resolved expression rank.                                    |
| `category` | string  | `"variable"` — an assignable designator (LHS of an assignment, an actual passed to an output dummy); `"constant"` — a folded compile-time value; `"expression"` — a computed value. |
| `value`    | string  | Folded value as a decimal integer string — only for scalar integer constants that fold.  Other constants (real, complex, logical) carry no `value`. |

### Source ranges

When emitted, `source` is an object:

```
"source": { "text": "x = 1 + 2",
            "file": "/abs/path/to/foo.f90",
            "line": 12, "col": 7,
            "endLine": 12, "endCol": 17 }
```

* `text` — the verbatim source slice spanning the node's `CharBlock`.
* `file` — absolute path of the originating source file.
* `line` / `col` / `endLine` / `endCol` — 1-based inclusive coordinates.

A node is missing `source` when its parse-tree class has no `source`
member (most "wrapper" containers — see [Transparent
wrappers](#transparent-wrappers)).

### Attributes

`attrs` entries are the result of `Fortran::semantics::AttrToString`
folded to lowercase.  The full set:

| Source spelling           | `attrs` entry           |
| ------------------------- | ----------------------- |
| `INTENT(IN)`              | `"intent(in)"`          |
| `INTENT(OUT)`             | `"intent(out)"`         |
| `INTENT(INOUT)`           | `"intent(inout)"`       |
| `OPTIONAL`                | `"optional"`            |
| `POINTER`                 | `"pointer"`             |
| `ALLOCATABLE`             | `"allocatable"`         |
| `TARGET`                  | `"target"`              |
| `SAVE`                    | `"save"`                |
| `VALUE`                   | `"value"`               |
| `PARAMETER`               | `"parameter"`           |
| `EXTERNAL`                | `"external"`            |
| `INTRINSIC`               | `"intrinsic"`           |
| `PURE`                    | `"pure"`                |
| `ELEMENTAL`               | `"elemental"`           |
| `IMPURE`                  | `"impure"`              |
| `RECURSIVE`               | `"recursive"`           |
| `MODULE`                  | `"module"`              |
| `NON_RECURSIVE`           | `"non_recursive"`       |
| `PROTECTED`               | `"protected"`           |
| `ASYNCHRONOUS`            | `"asynchronous"`        |
| `VOLATILE`                | `"volatile"`            |
| `CONTIGUOUS`              | `"contiguous"`          |
| `PUBLIC` / `PRIVATE`      | `"public"` / `"private"` |
| `BIND(C)`                 | `"bind_c"`              |
| `ABSTRACT`                | `"abstract"`            |
| `DEFERRED`                | `"deferred"`            |
| `NON_OVERRIDABLE`         | `"non_overridable"`     |
| `NOPASS`                  | `"nopass"`              |
| `PASS`                    | `"pass"`                |

The set is consolidated from every declaration that contributes to the
symbol — so a standalone `INTENT(IN) :: x` after a separate `REAL :: x`
produces the same `attrs` as the inline `REAL, INTENT(IN) :: x`.  This
is the field of choice when a consumer wants to ask "is this dummy
optional?" — walking the parse-tree `AttrSpec` would miss the
standalone-statement forms.

### Transparent wrappers

Several parse-tree classes are containers with no semantic content of
their own (`CharBlock`, `Statement<T>`, `UnlabeledStatement<T>`,
`common::Indirection<T>`, `std::tuple<…>`, `std::variant<…>`).  These do
not produce JSON nodes.  Their children appear directly under the
enclosing node, so the JSON tree is shorter than the C++ class
hierarchy.

The one exception is `Statement<T>`: if the source attached a numeric
statement label, it is hoisted onto the *inner* statement's JSON object
as the `label` field.  The wrapper itself is still elided.

### String escaping

`fortran` and `source.text` strings JSON-escape:

* `"` → `\"`
* `\` → `\\`
* `\b` `\f` `\n` `\r` `\t` → conventional two-char escapes
* Bytes `< 0x20` not covered above → `\u00XX`

All other bytes pass through unchanged — including non-ASCII source
characters (which are preserved as their UTF-8 byte sequences, on the
assumption that downstream tools treat the JSON as UTF-8).

## Stability

The schema described above is the *current* shape, not a stable
contract.  Consumers should:

* Treat unknown fields as additive (forward-compatible — accept and
  ignore).
* Expect new `attrs` entries when flang gains new attributes.
* Expect new `kind` values when the parse tree gains new node classes.
* Not depend on field *order* within a node.

If the format ever changes incompatibly (a field's type or meaning
changes), the action's option name will change too, so a consumer that
pins the option name will see a hard failure rather than silent
miscompare.

## Examples

A minimal one-statement program:

```fortran
program p
  integer :: j = 25
end program
```

emits, pretty-printed:

```
{"kind":"Program","children":[
  {"kind":"ProgramUnit","children":[
    {"kind":"MainProgram","children":[
      {"kind":"ProgramStmt","source":{...},"fortran":"P","children":[
        {"kind":"Name","source":{...},"fortran":"P","type":"...","proc":true}]},
      {"kind":"SpecificationPart","children":[
        ...
        {"kind":"EntityDecl","children":[
          {"kind":"Name","source":{...},"fortran":"j",
           "type":"INTEGER(4)","rank":0,"object":true},
          {"kind":"Initialization","children":[
            {"kind":"DataStmtConstant","fortran":"25_4",
             "type":"INTEGER(4)","rank":0,
             "category":"constant","value":"25",
             ...}]}]}]},
      ...]}]}]}
```

The `j` Name carries `type` / `rank` / `object` from the resolved symbol;
the `25` literal carries `type` / `rank` / `category` / `value` from the
analyzed expression — both reachable without re-running semantics.

## See also

* `-fdebug-dump-parse-tree` — the canonical text-form parse-tree dump.
* `-fdebug-dump-symbols` — the resolved-symbol table only.
* `-fdebug-dump-analyzed-tree-json-no-sema` — structure-only JSON for
  programs semantics would reject.
