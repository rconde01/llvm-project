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
4. **Minimize heuristics.** The converter must prefer *facts* that
   flang's semantic analysis already computed over re-deriving them with
   guesswork.  When the converter needs to know something the front end
   knows — a variable's resolved type and kind, its rank, whether a
   `name(...)` is an array element or a procedure call, whether a name is
   a local vs module/host-associated state — that information is exposed
   by the JSON parse-tree dumper (see `dump-parse-tree-json.h`, which
   emits `type` / `rank` / `object` / `proc` / `assoc` on `Name` nodes
   from the resolved `Symbol`) and read directly.  **Extend the dumper
   rather than add a heuristic to the converter.**  The default
   implicit-typing (I-N) rule survives only as a last-resort fallback for
   the rare name flang leaves untyped.

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

### R8 — I/O via standard C++ libraries (`fortran::io` only when needed)

**Default to plain standard-library I/O.**  `fortran::io::*` exists
only for cases where a Fortran edit descriptor can't be expressed
directly in `std::format`.  Generated code should read like
hand-written modern C++ for someone who has never seen Fortran.

  * `print *, …`            → `std::cout << … << '\n';`
  * `write(unit, *) …`      → `out << … << '\n';`
  * `read *, x`             → `std::cin >> x;`
  * `read(unit, *) x`       → `in >> x;`
  * `print '(I5)', x`       → `std::cout << std::format("{:5d}", x);`
  * `print '(F10.4)', x`    → `std::cout << std::format("{:10.4f}", x);`
  * `print '(G12.5)', x`    → `std::cout << fortran::io::fmt_G(x, 12, 5);`
                              (`G` has no `std::format` equivalent)
  * File `open` / `close`   → `std::ofstream` / `std::ifstream`
                              held in the state struct the routine
                              actually uses (no central unit-number
                              map unless multiple routines share it)

**Policy:** the emitter first tries to lower a Fortran edit
descriptor to an inline `std::format` spec.  Only when that fails —
`G`, `P` scale factors, `BN`/`BZ`, `T*`, `S*`, parenthesized repetition
groups, `$` carriage control — does it fall back to a
`fortran::io::fmt_*` helper.  See D5 for details.

---

## Open design questions

These are the choices that have real trade-offs.  Pick one each before
the first emitter is written.

### D1 — Array class implementation **(resolved: roll our own, C++20)**

We will hand-write `fortran::Array<T, Rank>` targeting C++20 (no
`std::mdspan` dependency).  Key responsibilities:

  * Runtime per-dimension lower bounds (default 1) and extents.
  * Column-major storage; `data()` returns a raw `T*` so BLAS/LAPACK
    interop is trivial.
  * `operator()(i)`, `operator()(i, j)`, … with the same arity and
    1-based indexing as Fortran.  Bounds-checked in debug, raw offset
    arithmetic in release.
  * Owning (`Array`) and non-owning (`ArrayRef`) flavors so we can
    pass slices without copying.
  * Move-only ownership semantics; copying is an explicit `clone()`
    to keep the cost visible.

### D2 — Where does the program's state live? **(resolved: D2.b — granular per-routine state)**

Each subprogram declares only the state it actually touches.  The
translator computes the per-subprogram read/write set from the AST
plus the call graph (we already have the pieces in `flang-ast-py`)
and emits signatures that take exactly those state structs.  Routines
that touch no state stay parameter-free.

Three reasonable shapes were considered.  Examples below use this
tiny Fortran program throughout so the differences are easy to
compare:

```fortran
module physics
  real :: gravity = 9.8
contains
  subroutine apply_gravity(v, dt)
    real, intent(inout) :: v
    real, intent(in)    :: dt
    v = v - gravity * dt
  end subroutine
end module

program sim
  use physics
  common /state/ position, velocity
  real :: position, velocity, dt
  integer :: step
  save  :: step

  position = 0.0;  velocity = 10.0;  dt = 0.1
  call apply_gravity(velocity, dt)
  position = position + velocity * dt
  print *, position, velocity
end program
```

#### D2.a — One context object

Every subprogram takes a single `Context&`.  All state lives on it.

```cpp
struct ProgramState {
  struct PhysicsModule { float gravity = 9.8f; }   physics;
  struct StateCommon   { float position, velocity; } state;
  struct SimSave       { int step; }                sim_save;
};

void apply_gravity(ProgramState& ctx, float& v, float dt) {
  v = v - ctx.physics.gravity * dt;
}

void sim(ProgramState& ctx) {
  ctx.state.position = 0.0f;
  ctx.state.velocity = 10.0f;
  auto dt = 0.1f;
  apply_gravity(ctx, ctx.state.velocity, dt);
  ctx.state.position = ctx.state.position + ctx.state.velocity * dt;
  std::cout << std::format("{} {}\n", ctx.state.position, ctx.state.velocity);
}

int main() {
  ProgramState ctx;
  sim(ctx);
}
```

*Pros:* one parameter to thread everywhere; trivial to add new state;
uniform call sites.  *Cons:* every signature pays the parameter cost
even when the body touches nothing; access reads `ctx.physics.gravity`
rather than just `gravity`; "what state does this routine actually
need?" is invisible.

#### D2.b — Granular per-routine state

Each subprogram declares only the state it touches.

```cpp
struct PhysicsModule { float gravity = 9.8f; };
struct StateCommon   { float position, velocity; };
struct SimSave       { int step; };

void apply_gravity(const PhysicsModule& physics, float& v, float dt) {
  v = v - physics.gravity * dt;
}

void sim(PhysicsModule& physics, StateCommon& state, SimSave& /*save*/) {
  state.position = 0.0f;
  state.velocity = 10.0f;
  auto dt = 0.1f;
  apply_gravity(physics, state.velocity, dt);
  state.position = state.position + state.velocity * dt;
  std::cout << std::format("{} {}\n", state.position, state.velocity);
}

int main() {
  PhysicsModule physics;
  StateCommon   state{};
  SimSave       save{};
  sim(physics, state, save);
}
```

*Pros:* signatures honestly advertise their dependencies; pure
routines stay pure; better unit-testability; `const` correctness is
trivial.  *Cons:* the translator has to compute the per-subprogram
read/write set (we already have most of the pieces from the call
graph); signatures change whenever a routine's state usage changes;
deep call chains can grow a lot of parameters.

#### D2.c — Class per module / program

Each Fortran module becomes a C++ class; the main program is also a
class.  Module variables become non-static members; module procedures
become member functions.  Cross-cutting things (common blocks, shared
state) are still standalone structs that get composed in.

```cpp
struct StateCommon { float position, velocity; };

class Physics {
public:
  float gravity = 9.8f;
  void apply_gravity(float& v, float dt) {
    v = v - gravity * dt;
  }
};

class Sim {
public:
  Sim(Physics& physics, StateCommon& state)
      : physics_(physics), state_(state) {}

  void run() {
    state_.position = 0.0f;
    state_.velocity = 10.0f;
    auto dt = 0.1f;
    physics_.apply_gravity(state_.velocity, dt);
    state_.position = state_.position + state_.velocity * dt;
    std::cout << std::format("{} {}\n", state_.position, state_.velocity);
  }

private:
  Physics&     physics_;
  StateCommon& state_;
  int          step_;        // save var, owned by Sim
};

int main() {
  Physics       physics;
  StateCommon   state{};
  Sim           sim(physics, state);
  sim.run();
}
```

*Pros:* idiomatic C++; modules feel like classes; "module variable"
becomes a plain member; testing a module in isolation is natural.
*Cons:* common blocks straddle modules and don't map to a single
owner; subprograms that don't belong to a module still need a home;
when a module USEs another module, the dependency becomes a member
reference and lifetime management is on the caller.

#### Trade-off summary

|                          | D2.a single ctx | D2.b granular | D2.c class-per-module |
|--------------------------|:---:|:---:|:---:|
| Translator complexity    | low | medium | medium |
| Signature noise          | medium (always one) | low (per use) | low (members) |
| Tells you what's touched | no  | yes | partly (only USE-deps) |
| Cross-module common      | trivial | trivial | awkward |
| Multiple instances       | ctor a new `ProgramState` | construct each struct | ctor a new `Sim` |
| `const` correctness      | only at object level | per parameter | per parameter |
| Feels like C++           | utilitarian | utilitarian | idiomatic |

### D3 — Character variable representation **(resolved: D3.c — hybrid)**

  * `std::string_view`  for read-only views — character literals
    (already R5) and `intent(in)` `CHARACTER` parameters.
  * `fortran::FortranString<N>`  for declared fixed-length variables
    (the common case).  Owns `std::array<char, N>` storage; assignment
    pads with blanks or truncates; equality is length-padded; the
    substring operator `name(lo, hi)` returns a writable proxy when
    used as an lvalue.
  * `std::string`  only when the Fortran source itself uses a
    variable-length representation (`character(len=:), allocatable`,
    deferred-length function results, etc.).

The hybrid keeps the type as informative as Fortran's was, costs no
extra heap for the common fixed-length case, and lets us pass
read-only character data through the program without ever forcing an
allocation.

#### Reasoning preserved for posterity

`std::string_view` (already mandated by R5 for character *literals*)
is not a candidate for character *variables*.  `string_view` is a
**non-owning** pointer + length: it can't be assigned to, can't be
resized, and refers to storage someone else owns.  A Fortran character
variable, by contrast, owns mutable storage with very specific
semantics:

  * **Fixed length** declared at the type level — `CHARACTER(LEN=10)`
    and `CHARACTER(LEN=20)` are different types, not "strings of
    different runtime length".
  * **Blank-padding on assignment** — `name = 'hi'` for a `LEN=10`
    name leaves `name` as `'hi        '` (8 trailing spaces), not
    `'hi'`.
  * **Truncation on overflow** — assigning `'this is too long'` to a
    `LEN=10` name keeps only the first 10 characters.
  * **Blank-padded equality** — `'hi' == 'hi        '` is **true** in
    Fortran (the shorter string is conceptually padded for the
    comparison) but **false** for `std::string`.
  * **Substring assignment** — `name(3:5) = 'XYZ'` mutates `name` in
    place; the type system has to know `name`'s declared length.

`string_view` provides none of those.  `std::string` provides the
storage but the wrong semantics (no padding, length-aware compare,
etc.).  We therefore need at least one purpose-built type for
character variables:

  * **D3.a — `std::string` everywhere.** Drop the fixed-length
    constraint.  Easiest to read; subtly wrong wherever
    blank-padding, length-aware comparison, or substring assignment
    matter.  Bugs are silent.
  * **D3.b — Custom `FortranString<N>`** fixed-length template
    storing `std::array<char, N>`, with assignment that pads /
    truncates, length-aware equality, and a `substr(lo, hi)` that
    returns a writable proxy when used as an lvalue.  Preserves
    semantics; types are noisier (the length is in the type).
  * **D3.c — Hybrid.** `std::string_view` for read-only views
    (literals, `intent(in)` parameters);  `FortranString<N>` for
    declared-length variables;  `std::string` only when Fortran
    itself uses variable-length (`character(len=:), allocatable`).
    Best fidelity, most concept variety.

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

### D5 — Formatted I/O **(resolved: inline `std::format` first, `fortran::io::*` only for descriptors that need fidelity)**

Goal: generated code reads like plain modern C++.  `fortran::io::*`
helpers exist only where they have to.

Emitter algorithm for each edit descriptor in a format string:

  1. **List-directed (`*` format)** → no formatting, just chain
     `<<` / `>>` on the standard stream.
  2. **`std::format`-equivalent descriptor**  (`I`, `F`, `E`,
     `A`, `L`, `Z`, integer-only `B`, plain `X` spacing) → inline
     `std::format("{:...}", x)`.  These are the descriptors that
     `std::format` can match byte-for-byte.
  3. **Otherwise** — `G` (general), `P` scale factor, `BN`/`BZ`
     blank interpretation, `T*` tab controls, `S*` sign controls,
     repetition with parenthesized groups, `$` carriage control →
     call a `fortran::io::fmt_*` helper.

The runtime exposes only the helpers that group (3) actually needs:

```cpp
namespace fortran::io {
  std::string fmt_G(double value, int w, int d,
                    std::optional<int> e = {});           // G edit descriptor
  std::string fmt_P(double value, int scale, char base,
                    int w, int d);                        // P scale factor
  std::string fmt_T(std::string_view buf, int col);       // tab to column
  // ...as the emitter encounters them.
}
```

So a `WRITE(unit, '(I5, 1X, F10.4)') i, x` lowers to

```cpp
out << std::format("{:5d}", i) << ' ' << std::format("{:10.4f}", x) << '\n';
```

with **no** runtime helper call.  Only descriptors that genuinely
have no `std::format` equivalent reach into `fortran::io::*`.

### D6 — Modules and `USE`

How do `USE`-imported names resolve at the C++ level?

  * Each Fortran module → C++ namespace + a "module state" struct.
  * `USE m, only : foo` → `using m::foo;` for procedures + an
    accessor for module variables on the state struct.
  * `USE m, foo => bar` (renaming) → `auto& foo = ...m_state.bar;` or
    a `using foo = m::bar;` for procedures.
  * Open: do we forbid wildcard `USE m`?  It expands to an
    enumeration of imported names — possibly hundreds.

### D7 — Untranslatable constructs **(partial: EQUIVALENCE resolved)**

#### EQUIVALENCE → `std::bit_cast` + byte buffer

Each `EQUIVALENCE` class becomes a struct with **one backing
`std::array<std::byte, N>`** sized to cover the union of the aliased
storages, plus **one accessor proxy per name** that reads / writes
its declared type into the buffer via `std::bit_cast`
(whole-value, same-size) or `std::memcpy` (offset or different-size
access).  Both are defined behavior — no strict-aliasing UB even
under `-O3`.

Example: same-size type pun

```fortran
real    :: x
integer :: bits
equivalence (x, bits)
```

```cpp
struct XBits_Equiv {
  std::array<std::byte, sizeof(float)> _store{};
  fortran::EquivSlot<float,         0> x   { _store.data() };
  fortran::EquivSlot<std::int32_t,  0> bits{ _store.data() };
};
```

Example: array overlap with offset

```fortran
real :: big(100), tail(10)
equivalence (big(91), tail(1))
```

```cpp
struct BigTail_Equiv {
  std::array<std::byte, 100 * sizeof(float)> _store{};
  fortran::ArrayRef<float, 1> big { _store.data(),  /*offset=*/ 0, /*extent=*/100 };
  fortran::ArrayRef<float, 1> tail{ _store.data(),  /*offset=*/90, /*extent=*/ 10 };
};
```

`EquivSlot<T, Offset>` is a runtime-support template that owns no
storage; it converts to/from `T` via `bit_cast` and a 1-line
`memcpy`-based load/store.  `ArrayRef` already takes a base pointer +
lower-bound + extent (R1 / D1), so the array-overlap case needs no
EQUIVALENCE-specific codepath in the array class.

#### Other untranslatable constructs

For everything else listed below, default policy is **D7.c — mixed**:

  * **Fail with diagnostic** (memory-unsafe or genuinely
    irreproducible control flow):
      * Assigned `GOTO` (`goto i` where `i` is a variable)
      * `ENTRY` statements (alternate entry points into a routine)
      * Alternate returns (`call foo(*100, *200)`)
  * **Emit `// TODO:` placeholder + clear comment** (awkward but
    safe; user can hand-finish):
      * `HOLLERITH` literals  →  comment + raw `char[]`
      * Computed `GOTO`       →  comment + `switch` skeleton
      * Fixed-form continuation past column 72  →  comment, drop
        the continuation
      * `FORALL`              →  comment + naive loop nest (deferred
        until we tackle vectorization)

Each fail-with-diagnostic case prints **what** the construct is,
**where** it appears (file:line:col), and a one-line suggestion (e.g.
"rewrite assigned GOTO as `select case`").

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
  * Decisions resolved:                                    D1, D2, D3, D5,
                                                           D7 (EQUIVALENCE
                                                           + escape-hatch
                                                           policy)
  * Decisions pending:                                     D4 (naming),
                                                           D6 (modules / USE)
  * Lowering pass:                                         **not started**
  * Emitter:                                               **not started**
  * Runtime support library (Array, FortranString, format helpers):
                                                           **not started**
