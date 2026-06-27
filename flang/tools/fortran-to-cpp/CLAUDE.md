# CLAUDE.md — fortran-to-cpp orientation

A guide for picking up this project cold.  Skim this before doing anything;
then read [`README.md`](README.md) for the design contract and
[`docs/CONSTRUCTS.md`](docs/CONSTRUCTS.md) for the per-construct catalog.

---

## What this is

A source-to-source translator that turns Fortran (FORTRAN 77 through
Fortran 2018) into readable, thread-safe C++20.  The frontend is **flang**
— we consume its JSON parse-tree dump (`-fdebug-dump-analyzed-tree-json`) via
the typed loader in [`flang-ast-py`](../flang-ast-py).  No code-gen
through LLVM IR; we walk the parse tree and emit C++ text.

The translator's two non-negotiables (from `README.md`):

1. **Read like the original** — same control flow, same variable names, same
   comments.
2. **Thread-safe** — no mutable globals, no `static` locals, no
   `thread_local`; every piece of state lives in an object the caller owns.

A third standing rule, especially relevant for adding features:

3. **Prefer flang's facts over heuristics.**  When the converter needs to
   know something flang's semantics already computed, **extend the dumper
   to emit it** rather than re-derive it in Python.  See README §"Goals"
   and §D8 for the rationale; see the recent commits adding `attrs` and
   expression `category`/`value` for the pattern.

---

## Repo layout

```
flang/include/flang/Parser/dump-analyzed-tree-json.h   ← JSON dumper (C++ header)
flang/tools/flang-ast-py/                           ← typed Python loader for the JSON
  flang_ast/nodes.py                                  Node dataclass + Symbol-attr fields
  flang_ast/parser.py, runner.py, annotate.py, depgraph.py, visitor.py
  tests/                                              flang-ast unit/integration tests
flang/tools/fortran-to-cpp/                         ← the converter
  converter/
    __init__.py        convert_file (single-file entry point)
    project.py         convert_files (multi-file: dependency order + whole-program passes)
    prepass.py         source-text sanitization before flang
    lowering.py        AST → IR (the bulk of the work)
    state_plumbing.py  whole-program passes: COMMON/SAVE/MODULE/WORKSPACE state threading
    emit.py            IR → C++ text
    ir.py              IR dataclasses (frozen where it can be)
    transform.py       map_statement / map_expr (visitor helpers)
    types.py, structure.py, ...
  runtime/include/fortran/                          ← runtime headers the emitted C++ #includes
    runtime.hpp, array.hpp, array_ref.hpp, string.hpp, intrinsics.hpp, io.hpp, system.hpp, ...
  tests/               unittest suite (one file per construct family)
  docs/
    CONSTRUCTS.md      every Fortran construct + the C++ it maps to + the trade-offs
    NONSTANDARD_SOURCE.md  source patches for files flang refuses to parse
  corpus_fixes/        standard-conforming patches mirroring the corpus tree
  README.md            design contract: Rules R1-R8 + resolved design questions D1-D8
  CLAUDE.md            this file
```

The corpus we test against lives outside the repo at `/tmp/fx`
(NASA SPICE + a handful of atmospheric models — IRI, MSIS, IGRF, …).

**Re-fetching the corpus** (the container wipes `/tmp` between sessions):
the canonical source is **https://github.com/rconde01/fortran_examples** —
clone it into `/tmp/fx` to restore the atmospheric example set (cira86,
igrf, iri_2001..2020, msis86/90, nrlmsis*, radbelt, …).  SPICE comes from
the NAIF toolkit tarball separately (see `docs/CORPUS_MEASUREMENT.md`).

---

## Quickstart commands

```bash
# Run the converter's test suite (unittest under pytest)
cd flang/tools/fortran-to-cpp
PYTHONPATH="$PWD:../flang-ast-py" FLANG=/home/user/llvm-project/build/bin/flang \
  /root/.local/bin/pytest tests/ -q -p no:cacheprovider

# Run the flang-ast typed-loader tests (includes dumper field tests)
cd ../flang-ast-py
FLANG=/home/user/llvm-project/build/bin/flang python3 -m unittest tests.test_flang_ast -v

# Rebuild flang (only TU that includes the dumper)
cd /home/user/llvm-project/build && ninja flang
```

Corpus measurement uses two ad-hoc scripts in `/tmp` (recreated each
session because `/tmp` gets wiped).  The exact source is preserved in
[`docs/CORPUS_MEASUREMENT.md`](docs/CORPUS_MEASUREMENT.md) — re-paste them
when you need to measure, then:

```bash
python3 /tmp/tool_measure_fixed.py component,support,spicelib support_fixed
CHUNK=100 python3 /tmp/tool_chunk.py /tmp/tool_out/support_fixed component,support,spicelib
```

Expected current state: **1625 files, 0 errors**.

---

## Pipeline in five passes

Read `converter/project.py::convert_files` top-to-bottom — it orchestrates
the whole pipeline.  The shape is:

1. **Parse + annotate** (per file, in `USE`-dependency order so `.mod`
   files are produced before they're consumed).  Output: `Node` trees with
   resolved-symbol attributes attached to every `Name` and analyzed `Expr`.

2. **Lowering** (`lowering.py::lower_program`, per file).  Walks the parse
   tree, emits an `IRTranslationUnit`.  This is where most translation
   decisions live: types, expressions, control flow, statement functions,
   declaration → `IRLocal` / `IRParameter`, etc.

3. **Whole-program passes** (`project.py`, after combining all files):

   - `_drop_external_function_locals` — drop `REAL F` decl-locals when
     `F` is actually a function defined elsewhere.
   - `_reshape_sequence_associated_args` — handle rank-mismatched actual
     args (sequence association).
   - `_infer_readonly_scalar_params` — F77 dummies that look read-only get
     `intent(in)` / `const T&`.  Skips `intent_declared` params (the
     declared intent is authoritative).
   - `_materialize_value_args` — wrap rvalue actuals in `ftn::byref`
     when the dummy is a modifiable scalar.  Now driven by AST `category`.

4. **State plumbing** (`state_plumbing.py::plumb_state`):

   - Builds per-routine structs for `COMMON`, `SAVE`, modules, and
     workspace (fixed-size local arrays).
   - Threads them as reference parameters through the call graph.
   - Wraps procedure actuals in **generic state-capturing lambdas** for
     dummy-procedure parameters.

5. **Emission** (`emit.py`):

   - Per-file `.cpp` (definitions of routines that aren't templates).
   - One shared header `fortran_modules.hpp` with structs, prototypes,
     and **template definitions** for procedure-taking routines.

---

## Cross-cutting design choices that bite if you don't know them

### Dummy procedures are function templates, not `std::function`

Any routine that takes a procedure dummy is emitted as `template <class
F0, ...> void foo(const F0& cb, ...)`, with its definition in the shared
header (not its own `.cpp`).  Every procedure *actual* is wrapped in a
**generic lambda** `[&](auto&&... a){ actual(state..., std::forward<…>(a)...); }`
— so even a higher-order routine (itself a template) passes deduction.

This means:
- There is **no cross-program dummy-procedure signature inference**.  The
  compiler deduces everything.
- A routine's definition's *file location* depends on whether it takes a
  procedure dummy.  Header for yes, `.cpp` for no.
- Compile time is noticeably slower because each `.cpp` instantiates the
  templates it uses.

See `README.md` §D8 and `docs/CONSTRUCTS.md` §"Dummy procedures".

### Storage association across mismatched argument types

F77's implicit interface lets an actual of one type bind a dummy of
another (a `DOUBLE PRECISION` array passed to an integer copier; a `double`
local used as an `int*` ID).  The runtime offers `ftn::storage_ref<To>`
and `ftn::reinterpret_array<To>` for this, inserted at call sites
**only where the C++ binding would otherwise fail to compile** (a non-const
ref to a different scalar type, or an `ArrayRef<U,R>` → `ArrayRef<T,R>`
with `U ≠ T`).  See `docs/CONSTRUCTS.md` §"Mismatched types".

### Entry-shared dummy procedures

A dummy procedure declared in one `ENTRY` is referenced by code that, in a
sibling entry's body, falls after a `RETURN` (dead, but must type-check).
That sibling declares an empty `std::function<R(args)>` *local* (a local
can't be a template parameter, so it falls back to the locally-observed
signature).  See `docs/CONSTRUCTS.md` §"ENTRY".

### SAVE-struct parameter hoist

When a `SAVE` local's array bound references a subprogram-local
PARAMETER constant, the constant is hoisted as a `static constexpr`
member of the SAVE struct itself.  The struct-scope field initializer
`Array<T,N> a{{maxsiz, ...}}` can then resolve `maxsiz` without seeing
the function body.  See the commented block at
`state_plumbing.py::_build_save_structs`.

---

## Recent significant work (last to first)

- **`6a18b4dea` `_materialize_value_args` reads AST category.**
  Drops the IR-shape pattern-match heuristic in favor of the
  authoritative `category` field flang attaches to every analyzed `Expr`.
- **`03ea1f40d` Dumper emits type/rank/category/value on `Expr`** (and
  any node carrying `typedExpr`).  See `dump-analyzed-tree-json.h`'s
  `EmitExprSemantics<T>` helper.
- **`c2a7918c0` SAVE struct hoists referenced PARAMETERs.**  Closes the
  standalone-`SAVE :: …` coverage gap.
- **`b4e500c2b` Read attrs / component+function types from the AST.**
  All symbol attributes (INTENT, OPTIONAL, SAVE, POINTER, …) flow through
  `name.attrs` — covers standalone attribute statements uniformly.
  `IRParameter.intent_declared` keeps declared `INTENT(INOUT)` from being
  demoted by inference.
- **`934718dca` Dummy procedures as templates + generic-lambda actuals.**
  Took the GF nested/forwarded-only cluster from 5 errors to 0; replaces
  the whole-program signature-inference machinery.
- **`e846a2dfe` / `11107bd23` `corpus_fixes/`** — standard-conforming
  patches for `txtopr`, `zzascii`, `iriorbit`, `iriorbitmax`, `irifun`,
  `radbelt`, `iri_imaz`.  With these substituted in, the SPICE corpus
  compiles **1625 files, 0 errors** end-to-end.

---

## Current status & known limits

- SPICE corpus (`component + support + spicelib` with `corpus_fixes/`
  substituted): **1625 files, 0 errors.**
- Test suite: **267 passing** (converter) + **15 passing** (flang-ast).
- Non-SPICE corpora (IRI, MSIS, IGRF, radbelt, CIRA) convert; a few
  individual files in `iri_2007/IMAZ`, `iri_2012`, and `radbelt` need the
  `corpus_fixes/` patches to parse.  See
  `docs/NONSTANDARD_SOURCE.md` and `corpus_fixes/README.md`.

Outstanding cleanups (none affect correctness):

- `PURE` / `ELEMENTAL` / `CONTIGUOUS` attrs are in `name.attrs` but the
  converter doesn't use them — could mark generated C++ functions
  `constexpr`-eligible / enable raw-pointer access.
- The whole-program `_resolve_component_allocations` and
  `_resolve_pointers` passes are now fallback-only (lowering reads
  resolved types directly).  Could be removed once a corpus run confirms
  they never fire.

---

## Operational notes (please read)

- **Container churn.**  This environment occasionally rolls back the
  working tree (sometimes the whole `HEAD`) mid-session.  Every time you
  start, **check `git status --short` and `git rev-parse HEAD vs
  origin/...`**.  If they diverge, recover with `git fetch origin <branch>
  && git merge --ff-only origin/<branch>`.  Always push commits soon — the
  remote is the safe copy.
- **`/tmp` gets wiped.**  Anything in `/tmp` (corpus measurement scripts,
  intermediate output) is ephemeral.  Reusable scripts live in
  `docs/CORPUS_MEASUREMENT.md`.
- **Dumper rebuilds.**  Only `flang/lib/Frontend/ParserActions.cpp`
  includes the JSON dumper header, so a header change is a quick
  rebuild — `ninja flang` from `build/` takes a couple minutes.
- **Don't push without a test pass.**  267 tests run in ~2 min — well
  worth it before every push.  Don't push converter changes without
  also running the corpus measurement; a broken dumper or pass can take
  the corpus from 0 errors to thousands.

---

## "I want to enhance X" — where to look

| Want to | Start at |
|---|---|
| add a new Fortran construct | `docs/CONSTRUCTS.md` (write the C++ target) → `lowering.py` (lower it) → `emit.py` (emit it) → write a test |
| change how a construct emits | `emit.py` (the emit path) + a focused emit-assertion test |
| add a runtime helper | `runtime/include/fortran/*.hpp` (mirror the style of `intrinsics.hpp` / `array_ref.hpp`) |
| extract a new fact from semantics | Add it to `dump-analyzed-tree-json.h` (see `EmitExprSemantics` as the template) → expose on `flang_ast/nodes.py::Node` → consume in `lowering.py` |
| handle a new attribute | It's probably already in `name.attrs`.  Read it in `_lower_type_declaration`. |
| simplify an inference pass | Audit per `README.md` §"Goals" #4 ("prefer flang's facts"); see if there's a dumper field that obviates it |
| handle a new IO descriptor | `runtime/include/fortran/io.hpp` + a focused test |

---

## What this file is not

Not a tutorial, not a complete API reference.  For "what does this
construct map to?", read `docs/CONSTRUCTS.md`.  For "what rules must the
emitted code obey?", read `README.md` §"Rules".  For "what changed
recently and why?", read the git log — commit messages are detailed.
