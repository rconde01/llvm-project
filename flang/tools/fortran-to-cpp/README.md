# fortran-to-cpp

A source-to-source translator that turns Fortran programs into modern
C++ by consuming the JSON parse tree produced by flang
(`-fdebug-dump-parse-tree-json`) via the typed loader in
[`flang-ast-py`](../flang-ast-py).

This document captures **the rules the generated code must follow** and
the **open design questions** we still need to settle before writing
any emitter code.  Treat the "Rules" section as a contract: every part
of the translator should honor it.  The "Open design questions"
section lists the architectural choices that have real trade-offs —
pick one per question before implementation starts.

---

## Goals

1. Produce C++ that **reads** like the Fortran original — same control
   flow, same variable names where reasonable, same comments,
   recognizable structure.  Performance is secondary to readability and
   correctness of the translation.
2. Produce C++ that is **safely usable from multiple threads** — no
   hidden mutable globals; every piece of state lives in an object the
   caller owns.
3. Make every kind of Fortran source artifact (statement, common
   block, save block, module variable, I/O) map onto a small, stable
   set of C++ patterns so the output is predictable and auditable.

---

## Rules (must hold for every emitted file)

### R1 — Custom array class, indexing preserved

Fortran arrays are 1-indexed by default, allow arbitrary lower bounds
(`integer :: a(0:9)`), and use column-major storage.  Emit a custom
`fortran::Array<T, Rank>` (name TBD) that preserves the **exact same
indexing expressions** as the Fortran source.

  * `a(i)`, `a(i,j)`, `a(i,j,k)` translate verbatim to `a(i)`,
    `a(i,j)`, `a(i,j,k)` in C++ via `operator()`.
  * Lower bounds default to 1 and are configurable per dimension.
  * The class is responsible for column-major layout so passing data
    to LAPACK/BLAS-style routines is straightforward.
  * **Open question** — see D1.

### R2 — Preserve comments from the original source

Use `flang_ast.annotate` to attach comments to AST nodes.  On output:

  * **Leading** comments → C++ `//` lines immediately above the
    corresponding statement / function, at matching indent.
  * **Trailing** (inline) comments → C++ `//` after the statement on
    the same line.
  * Multi-line blocks render as a contiguous block of `//` lines.
  * Directives (`!$OMP`, `!DIR$`, …) flagged by the annotator are
    routed separately (translation is deferred until we know the
    OpenMP/OpenACC strategy).

### R3 — Thread-safe by construction

The generated code must be safe to call from multiple threads with
**multiple simultaneous logical Fortran programs** running side by
side.  Concretely:

  * **No** mutable namespace-scope variables.
  * **No** `static` locals carrying program state.
  * **No** `thread_local` shortcut — that would make it impossible to
    run two independent program instances in the same thread.
  * Every piece of would-be-global state (common blocks, save
    variables, module variables) lives in an object owned by the
    caller and passed explicitly.

This rule is what forces R6, R7, and the state-strategy decision in
D2.

### R4 — `auto` everywhere except numeric literals

  * Use `auto` for variable initializers whose RHS is a function call
    or another expression of inferred type.
  * **Numeric literals** (Fortran has typed-kind literals like `1_4`,
    `1.0_8`, `1.0d0`) emit with an **explicit type** so kind is
    preserved through the translation.  The literal value still
    appears on the RHS; only the LHS declaration must be explicit.
  * Loop variables, return-type declarations, and parameters are
    explicit by their nature and don't use `auto`.

### R5 — `std::string_view` for string literals

Untyped Fortran character literals (`"hello"`) become
`std::string_view` (typically via the `""sv` UDL).  Strings stored in
character variables follow the rules in D3 below.

### R6 — Common blocks as structs

Each named common block (`common /name/ x, y, z`) translates to a
top-level `struct CommonName { ... };`.  An instance of that struct is
passed alongside other state so each program instance has its own
copy.  An unnamed `common` block becomes `struct BlankCommon { ... };`
(name TBD — see D4).

### R7 — `save` statements as structs

A subprogram's `save`d locals are bundled into a per-subprogram struct
`<Subprogram>Save` that the caller owns and passes in.  This keeps the
"state-is-explicit" invariant of R3.  Subprograms with no `save`
locals don't need such a struct.

### R8 — I/O via standard C++ libraries

  * `print *, …`            → `std::cout << … << "\n";`
  * `write(unit, *) …`      → `std::ostream& out = ...; out << … << "\n";`
  * `read *, x`             → `std::cin >> x;`
  * `read(unit, *) x`       → `std::istream& in = ...; in >> x;`
  * Formatted I/O (`FORMAT` statements)  → `std::format` (C++20) or
    `fmt::format` — see D5.
  * File I/O (`open`, `close`)  → `std::ofstream` / `std::ifstream`,
    held in a unit-number map within the context object (see D2).

---

## Open design questions

These are the choices that have real trade-offs.  Pick one each before
the first emitter is written.

### D1 — Array class implementation

How do we implement `fortran::Array<T, Rank>`?

  * **D1.a — Roll our own.** Hand-written template with runtime
    bounds and column-major indexing.  Maximum control; minimum
    external dependencies.  More code to maintain.
  * **D1.b — Build on `std::mdspan` (C++23).** Use `mdspan` as the
    storage view and wrap it with a Fortran-style `operator()` that
    applies the lower-bound offsets.  Less code to write, but requires
    a C++23 toolchain.
  * **D1.c — Compile-time bounds when possible, runtime otherwise.**
    Two specializations (`StaticArray<T, N1, N2, ...>` vs
    `DynamicArray<T, Rank>`) selected by the emitter based on whether
    the bounds are constant expressions.  Best codegen, most surface
    area.

### D2 — Where does the program's state live?

The translation needs to pass common blocks, save structs, module
state, and I/O units somewhere.  Two reasonable shapes:

  * **D2.a — One context object.** A single `Context` (or
    `ProgramState`) struct passed by reference to every subprogram.
    It holds all common blocks, save structs, module-variable structs,
    and the unit→stream map.  Subprogram signatures grow by exactly
    one parameter; cross-cutting access is uniform.  Easy to extend;
    every routine pays the parameter cost even when it touches no
    global state.
  * **D2.b — Granular structs, explicit dependencies.** Each
    subprogram takes only the structs it actually touches.  Routines
    that need no state take none.  Signatures are honest;
    dependencies are visible at the call site.  Translator has to
    compute the per-subprogram dependency set (we already have the
    pieces for this — the call graph plus a per-subprogram "what does
    this routine read/write" pass).
  * **D2.c — Class wrapper per module/program.** Each Fortran module
    becomes a C++ class; its module variables become non-static
    members; module procedures become member functions.  The main
    program becomes a class whose constructor takes any inputs.
    Natural OO mapping; needs an extra rule for what owns the
    cross-module common blocks.

### D3 — Character variable representation

Fortran character variables have a fixed length set at declaration
time and are blank-padded on assignment.  Translation options:

  * **D3.a — `std::string` everywhere.** Drop the fixed-length
    constraint.  Easiest to read; subtly wrong for code that depends
    on blank-padding semantics or substring assignment.
  * **D3.b — Custom `FortranString<N>`** fixed-length template
    storing `std::array<char, N>`, with assignment that pads or
    truncates exactly as Fortran does.  Preserves semantics; uglier
    types.
  * **D3.c — Hybrid.** Use `std::string_view` for read-only views,
    `FortranString<N>` for declared-length variables, `std::string`
    only for genuinely variable-length cases (`character(len=:),
    allocatable`).

### D4 — Naming policy

Lots of small naming questions; settle them once so the output is
consistent:

  * Fortran identifiers are case-insensitive and (after sema)
    uppercased internally.  Do we emit them as `original_lowercase`,
    `lowercase` (canonical), `snake_case`, or preserve the user's
    original spelling exactly?
  * Blank common block → `BlankCommon`?  `UnnamedCommon`?
    `Common0`?
  * Save struct name → `<Subprogram>Save`?  `<Subprogram>State`?
  * Module namespace name → match Fortran spelling, or lowercase?

### D5 — Formatted I/O

Fortran `FORMAT` strings are powerful (edit descriptors `I5`, `F10.4`,
`Ew.dEe`, repetition, etc.).  We need a translation target.

  * **D5.a — `std::format`** (C++20).  Translate each Fortran edit
    descriptor to the closest `std::format` specifier.  Some
    descriptors don't map cleanly (e.g. `G`, `P` scale factors).
  * **D5.b — `fmt::format`** (external library).  Same as above but
    with the {fmt} library, which has wider behavior and is available
    on older toolchains.
  * **D5.c — Helper functions.** Emit calls to small format helpers
    in our own runtime support library that implement Fortran's exact
    edit descriptors.  Largest behavioral fidelity; biggest runtime.

### D6 — Modules and `USE`

How do `USE`-imported names resolve at the C++ level?

  * Each Fortran module → C++ namespace + a "module state" struct.
  * `USE m, only : foo` → `using m::foo;` for procedures + an
    accessor for module variables on the state struct.
  * `USE m, foo => bar` (renaming) → `auto& foo = ...m_state.bar;` or
    a `using foo = m::bar;` for procedures.
  * Open: do we forbid wildcard `USE m`?  It expands to an
    enumeration of imported names — possibly hundreds.

### D7 — Untranslatable constructs

We will hit Fortran features that don't have a clean C++ analogue:

  * `EQUIVALENCE` (memory aliasing)
  * `ENTRY` statements (alternate entry points)
  * Computed/assigned GOTO
  * Alternate returns
  * `HOLLERITH` and other ancient literals
  * Fixed-form continuation past column 72

Decide up front:

  * **D7.a — Fail with a clear diagnostic** and refuse to emit.
  * **D7.b — Emit a `// TODO: translate <construct>` placeholder** so
    the user can patch by hand.
  * **D7.c — Mixed:** fail on memory-unsafe constructs (EQUIVALENCE,
    assigned GOTO), `TODO` on awkward but safe ones.

---

## Pipeline sketch

Once D1–D7 are settled, the converter will run as:

  1. **Parse**     — drive flang, get JSON AST (already done in
     `flang-ast-py`).
  2. **Annotate**  — attach comments (already done).
  3. **Order**     — topologically sort subprograms (already done).
  4. **Lower**     — walk the AST, build an intermediate model that
     records every name, type, scope, and I/O unit.  This is where
     the D-decisions show up.
  5. **Emit**      — pretty-print C++ from the intermediate model,
     preserving comments and indentation.

Each stage is a separate Python module so we can test in isolation
and so the intermediate model is inspectable.

---

## Status

  * `flang-ast-py` (parse + annotate + dependency order):  **done**
  * D1–D7 decisions:                                       **pending**
  * Lowering pass:                                         **not started**
  * Emitter:                                               **not started**
  * Runtime support library (Array, FortranString, format helpers):
                                                           **not started**
