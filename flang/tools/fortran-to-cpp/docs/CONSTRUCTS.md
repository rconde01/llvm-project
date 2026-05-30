# Fortran → C++ construct catalog

This document shows how `fortran-to-cpp` translates each Fortran
construct, with a minimal Fortran example and the C++ the converter
actually produces. Where a construct has more than one reasonable C++
mapping, the **Design** note lists the alternatives, their trade-offs,
and why this translator picks the one it does.

The two guiding goals (see [`README.md`](../README.md)) shape almost
every choice:

1. **Read like the original** — same control flow and names where
   reasonable.
2. **Thread-safe** — no hidden mutable globals; every piece of state
   lives in an object the caller owns.

All C++ snippets below omit the generated file header and `#include`
block for brevity.

---

## Contents

- [Program structure](#program-structure)
- [Variables and implicit typing](#variables-and-implicit-typing)
- [Counted DO loop](#counted-do-loop)
- [DO WHILE](#do-while)
- [Block IF](#block-if)
- [SELECT CASE](#select-case)
- [GOTO and friends](#goto-and-friends)
- [Arrays](#arrays)
- [Array sections and whole-array assignment](#array-sections-and-whole-array-assignment)
- [Characters and substrings](#characters-and-substrings)
- [Subroutines, functions, and argument intent](#subroutines-functions-and-argument-intent)
- [Argument copy-in](#argument-copy-in)
- [Statement functions](#statement-functions)
- [Dummy procedures](#dummy-procedures)
- [COMMON blocks](#common-blocks)
- [SAVE variables](#save-variables)
- [DATA statements](#data-statements)
- [Derived types](#derived-types)
- [ENTRY](#entry)
- [Assumed-size array dummies](#assumed-size-array-dummies)
- [Sequence and storage association](#sequence-and-storage-association)
- [Assumed-length CHARACTER dummies](#assumed-length-character-dummies)
- [Assumed-length CHARACTER arrays (character cells)](#assumed-length-character-arrays-character-cells)
- [Names that shadow a library routine](#names-that-shadow-a-library-routine)
- [INCLUDE files](#include-files)

---

## Program structure

Every program unit becomes a free function; the Fortran main program
becomes a function called from a tiny `int main()` wrapper.

```fortran
program hello
  print *, "Hello, world"
end program
```

```cpp
void hello() {
  std::cout << "Hello, world"sv << '\n';
}

int main() {
  hello();
  return 0;
}
```

**Design — why a wrapper instead of putting the body straight in
`main`?** A uniform "one program unit → one function" rule keeps the
output predictable: the main program is plumbed for state, called, and
forwarded exactly like any subroutine, so there are no special cases in
the emitter or the state-threading pass.

---

## Variables and implicit typing

Resolved types come from flang's symbol table, so KINDs and custom
`IMPLICIT` rules are honored exactly. Locals are value-initialized
(`{}`).

```fortran
program p
  integer :: n
  real(kind=8) :: x
  n = 42
  x = 3.5d0
end program
```

```cpp
void p() {
  std::int32_t n{};
  double x{};

  n = 42;
  x = 3.5e0;
}
```

The FORTRAN 77 implicit rule (names starting `I`–`N` are integer, the
rest real) is applied **only** to names flang left untyped — it is a
last-resort fallback, not the primary source of types.

Integer literals are decimal in Fortran but a leading zero makes C++ read
them as octal, so `08` (invalid octal) is normalized to `8` during
lowering; `1.0d0` double-precision literals become `1.0e0`.

---

## Counted DO loop

```fortran
do i = 1, 5
  s = s + i
end do
```

```cpp
for (i = 1; i <= 5; ++i) {
  s = s + i;
}
```

A non-unit step that isn't a known-positive constant uses a
direction-aware test so the loop still runs when counting down:

```fortran
do i = 10, 2, -2
  print *, i
end do
```

```cpp
for (i = 10; (-2 >= 0 ? i <= 2 : i >= 2); i += -2) {
  std::cout << i << '\n';
}
```

**Design.** A literal positive step emits the plain `i <= hi` test; only
a step whose sign is unknown pays for the `(step >= 0 ? … : …)` guard,
keeping the common case clean.

---

## DO WHILE

```fortran
do while (i < 5)
  i = i + 1
end do
```

```cpp
while (i < 5) {
  i = i + 1;
}
```

---

## Block IF

```fortran
if (n == 1) then
  print *, "one"
else if (n == 2) then
  print *, "two"
else
  print *, "other"
end if
```

```cpp
if (n == 1) {
  std::cout << "one"sv << '\n';
} else if (n == 2) {
  std::cout << "two"sv << '\n';
} else {
  std::cout << "other"sv << '\n';
}
```

---

## SELECT CASE

```fortran
select case (n)
case (1)
  print *, "one"
case (2, 3)
  print *, "two/three"
case default
  print *, "other"
end select
```

```cpp
if (n == 1) {
  std::cout << "one"sv << '\n';
} else if (n == 2 || n == 3) {
  std::cout << "two/three"sv << '\n';
} else {
  std::cout << "other"sv << '\n';
}
```

**Design — `if`-chain vs. C++ `switch`.** A C++ `switch` looks closer to
`SELECT CASE`, but it can't express case *ranges* (`case (1:5)`), needs
`break` on every arm, and requires an integral selector. The `if`-chain
handles ranges (`sel >= 1 && sel <= 5`), character and logical
selectors, and falls through naturally — one lowering covers every form
of `SELECT CASE`. A simple selector expression is referenced directly; a
compound one is bound to a `const auto _sel = …;` temporary first so it
is evaluated once.

---

## GOTO and friends

The structuring pass eliminates `goto` entirely — the generated C++
never contains the `goto` keyword. A forward "skip" becomes an `if`:

```fortran
      if (n .gt. 0) goto 10
      n = -1
10    n = n + 1
```

```cpp
if (!(n > 0)) {
  n = -1;
}

n = n + 1;
```

A backward branch (a loop built from labels) becomes a small
program-counter dispatch loop:

```fortran
      i = 0
10    i = i + 1
      if (i .lt. n) goto 10
```

```cpp
int _pc{};
_pc = 0;
while (_pc != 2) {
  if (_pc == 0) {
    i = 0;
    _pc = 1;
  } else if (_pc == 1) {
    i = i + 1;
    if (i < n) {
      _pc = 1;
      continue;
    }
    _pc = 2;
  }
}
```

**Design — three options for arbitrary `goto`:**

| Approach | Pro | Con |
|---|---|---|
| Native C++ `goto` | shortest | C++ `goto` can't jump across a variable's initialization; spaghetti survives into the output |
| Structured reconstruction (if/loop) | most readable | only works for *reducible* control flow |
| `_pc` dispatch loop | works for **any** label graph; no `goto` | reads less naturally than a real loop |

The converter applies structured reconstruction wherever the control
flow is reducible (the common `if (c) goto`, counted loops) and falls
back to the `_pc` dispatch loop only for the irreducible remainder. This
keeps the easy 90% readable while guaranteeing *every* `goto` graph
translates without resorting to a C++ `goto`. Arithmetic `IF` and
computed `GOTO` lower to ordinary branches feeding the same machinery.

---

## Arrays

Arrays use the runtime's `fortran::Array<T, Rank>` (owning) and
`fortran::ArrayRef<T, Rank>` (non-owning view). Indexing is **1-based**
and storage is **column-major**, matching Fortran exactly.

```fortran
real :: v(3), m(2,2)
v(1) = 1.0
m(2,1) = 4.0
```

```cpp
fortran::Array<float, 1> v{{3}};
fortran::Array<float, 2> m{{2, 2}};

v(1) = 1.0f;
m(2, 1) = 4.0f;
```

**Design — why a custom array type?**

| Approach | Problem |
|---|---|
| `std::vector<T>` | 1-D only; 0-based; no column-major multidim indexing; no Fortran bounds |
| `std::mdspan` (C++23) | a *view* only — doesn't own storage, no bounds checking, no whole-array ops |
| raw `T*` + manual index math | loses bounds, lower bounds, and shape; unreadable |

`fortran::Array` bakes in the four things Fortran assumes and C++ does
not: 1-based subscripts, arbitrary lower bounds (`a(0:9)`), column-major
layout, and shape-aware whole-array operations. `ArrayRef` is the dummy
form — a small view that a caller's owning `Array` converts to
implicitly, so a subroutine can take any slice without copying.

---

## Array sections and whole-array assignment

A whole-array elementwise statement expands into an explicit loop nest
(no temporaries):

```fortran
v = v + 1.0
```

```cpp
for (fortran::index_t _i1 = v.lbound(1); _i1 <= v.ubound(1); ++_i1) {
  v(_i1) = v(_i1) + 1.0f;
}
```

A section-to-section copy maps the source and destination by position:

```fortran
b(1:3) = a(3:5)
```

```cpp
for (fortran::index_t _k2 = 0; _k2 <= 3 - 1 + 1 - 1; ++_k2) {
  b(1 + _k2) = a(3 + _k2);
}
```

**Design — loop expansion vs. operator overloading.** `fortran::Array`
*does* define elementwise operators, so `v = v + 1.0` could be emitted
verbatim. But chained array expressions (`d = a + b*c`) would then
allocate a temporary per operator. Expanding elementwise statements into
a single loop fuses the whole expression with zero temporaries, which
also reads like the loop a Fortran programmer would have written by
hand. A *whole-array* assignment whose right side is itself an
array-valued result (`a = matmul(x, y)`, or a rank-≥2 section copy
`a = b(:,:,k)`) is left as a single `operator=` call, since there is no
per-element scalar work to fuse and the runtime does the shape-checked
copy.

---

## Characters and substrings

A `CHARACTER(len=N)` is a fixed-length, blank-padded
`fortran::FortranString<N>`.

```fortran
character(len=5) :: name
name = "abc"
print *, name
```

```cpp
fortran::FortranString<5> name{};
name = "abc"sv;
std::cout << name << '\n';
```

A substring is a 1-based inclusive slice; an omitted bound defaults to
`1` / the length (`fortran::len`, which works for any character base):

```fortran
print *, s(1:5)
print *, s(7:)
```

```cpp
std::cout << s(1, 5) << '\n';
std::cout << s(7, fortran::len(s)) << '\n';
```

The base can be any character designator, not just a name — a substring
of a character-array element (a "cell" element) composes:

```fortran
out(l)(c:c) = array(l)(c:c)
```

```cpp
out(l)(c, c) = array(l)(c, c);   // each a(i) is a CharRef; (c,c) slices it
```

**Design — `FortranString<N>` vs. `std::string`.** Fortran `CHARACTER` is
fixed-length and blank-padded: assigning a shorter value pads with
spaces, a longer value truncates, and the length is part of the type.
`std::string` is variable-length and would silently get all of that
wrong. The fixed-size type also lives inline in COMMON/derived-type
structs with the right storage size.

**Named-constant lengths.** The length need not be a literal — it is
often a `PARAMETER`:

```fortran
integer, parameter :: nwc = 1024
character*(nwc) :: rcbufc(bufszc)
```

```cpp
fortran::Array<fortran::FortranString<1024>, 1> rcbufc{{bufszc}};
```

The declaration's syntax tree only carries the symbolic length `NWC`, so
the element type is taken from flang's *resolved* symbol type
(`CHARACTER(1024,1)`) rather than the raw spec; that substitution is what
turns `nwc` into `1024`. Falling back to the raw spec would have yielded
`FortranString<1>` and mismatched every call that passes `rcbufc` to a
`CHARACTER*(NWC)` dummy.

---

## Subroutines, functions, and argument intent

FORTRAN 77 has no `INTENT`, so the converter infers it. A scalar dummy
that is never assigned (directly or transitively through a callee that
writes it) is passed by `const&`; one that is written is passed by `&`.

```fortran
subroutine axpy(a, x, y)
  real :: a, x, y
  y = a * x + y
end subroutine
```

```cpp
void axpy(const float& a, const float& x, float& y) {
  y = a * x + y;
}
```

A function returns its result through a local named `<name>_result`:

```fortran
real function square(x)
  real :: x
  square = x * x
end function
```

```cpp
float square(const float& x) {
  float square_result{};
  square_result = x * x;
  return square_result;
}
```

**Design — intent inference vs. "everything is `&`".** Passing every
scalar by mutable reference would always be *correct*, but then a caller
could not pass a literal or an expression (`call axpy(2.0, x, y)`), and
the signatures would hide which arguments are actually outputs. A
whole-program fixpoint marks a parameter non-const if it is assigned
anywhere it flows, and `const&` otherwise — recovering both readability
and the ability to pass rvalues. The inference is conservative: when in
doubt, a parameter stays mutable, so it never silently drops a needed
write-back.

---

## Argument copy-in

Fortran lets *any* expression be an actual argument; for a modifiable
dummy it binds a temporary and discards the write-back. C++ refuses to
bind a non-`const` `T&` to an rvalue, so the converter routes such
actuals through `fortran::byref`:

```fortran
call add_one(2.0 + 3.0)   ! add_one writes its dummy
```

```cpp
add_one(fortran::byref(2.0f + 3.0f));
```

`fortran::byref` materializes the value into an lvalue whose lifetime
spans the call. **Design — helper vs. a hoisted named temporary.** A
named temp (`float _t = 2.0f + 3.0f; add_one(_t);`) would also work, but
needs statement-level rewriting and a fresh name; `byref` keeps the call
a single expression and is a no-op for lvalue actuals (so a genuine
variable still receives its write-back).

---

## Statement functions

A statement function becomes a generic lambda capturing the host by
reference:

```fortran
      sq(y) = y * y
      f = sq(x) + 1.0
```

```cpp
auto sq = [&](auto y) { return y * y; };
f_result = sq(x) + 1.0f;
```

**Design — lambda vs. a free helper function.** A statement function may
read the host routine's locals, so the lambda captures `[&]`. The
generic `auto` parameter sidesteps having to reconstruct the dummy's
type and lets the same lambda accept any numeric argument, mirroring the
loose typing of a statement function.

---

## Dummy procedures

A procedure passed as an argument (`EXTERNAL f`, then `f(x)` inside) makes
the receiving routine a **function template**: each dummy procedure is a
*deduced* type parameter, not a fixed `std::function`. At the call site the
actual routine is wrapped in a state-capturing **generic lambda**.

```fortran
real function apply(f, x)
  real :: f, x
  external f
  apply = f(x)
end function
...
  r = apply(square, 3.0)
```

```cpp
template <class F0>
float apply(const F0& f, const float& x) {
  float apply_result{};
  apply_result = f(x);
  return apply_result;
}
...
r = apply([&](auto&&... _a) { return square(std::forward<decltype(_a)>(_a)...); }, 3.0f);
```

The template definition lives in the **shared header** (a template must be
visible at every call site), and the per-file `.cpp` skips it; the prototype
is a template prototype, so a template that calls another template resolves
regardless of order.

**Why this shape — `std::function` vs. template vs. function pointer.**

| Approach | Trade-off |
|---|---|
| function pointer | zero overhead, but **cannot** carry the state-capturing lambda the actual routine needs (threaded COMMON/workspace state) |
| `std::function<R(args)>` | one uniform, separately-compilable parameter type — but it pins down `R(args)`, so the converter must *infer* the exact arity/types of a callback it has no interface for |
| **template `<class F>`** *(used here)* | the compiler deduces the callback type from the actual; **no inference at all**. Cost: the definition must sit in the header (no separate compilation of the callee) and instantiates per call-type |

The deciding factor is that **FORTRAN 77 has no interface block** for a
dummy procedure, so the receiving routine genuinely does not know its
signature. With `std::function` the converter had to reconstruct that
signature across the whole call graph — and a procedure that is only
*forwarded* (passed straight on, never called locally) offered no local
clue to its arity or types, and a *nested* callback (a procedure whose own
argument is a procedure) compounded the problem. Letting the compiler
deduce the type sidesteps all of it.

**The generic-lambda wrapper is what makes deduction always work.** Two
things must hold for `apply(actual)` to deduce a usable type for `F`:

1. the actual must be a *concrete object* with a type — but a procedure
   that itself takes a dummy procedure is, after this transform, a
   *template*, and a template name has no type to deduce from
   (`decltype(gfevnt)` is ill-formed);
2. the actual must carry its captured state.

Wrapping every actual in `[&](auto&&... _a){ actual(state…, _a…); }` solves
both: the lambda is a concrete closure (so deduction has something to latch
onto, even for a higher-order routine), it captures state by reference, and
being *generic* it imposes no fixed signature — the callee calls it with
whatever arguments it likes and they forward straight through.

```fortran
c     GFEVNT forwards UDREFN/UDBAIL onward; ZZGFREL nests a scalar
c     function inside a boolean callback — neither needs a declared type.
      call gfevnt ( gfstep, gfrefn, ..., gfbail, result )
```

```cpp
// No inferred signatures — every callback is a deduced template parameter,
// every actual a generic lambda:
gfevnt(..., [&](auto&&... _a){ gfrefn(std::forward<decltype(_a)>(_a)...); },
       ..., [&](auto&&... _a){ return gfbail(std::forward<decltype(_a)>(_a)...); },
       result);
```

This handles forwarded-only chains and arbitrarily nested
procedure-of-procedure callbacks uniformly, because at no point does the
converter need to name a callback's type — the C++ compiler deduces each
one from the closure that is actually passed.

> A procedure stored in a *local* (not a parameter) — e.g. a sibling
> ENTRY's dummy procedure referenced in dead code — can't be a template
> parameter, so it falls back to an empty `std::function` of the locally
> observed call shape (it is never actually invoked). See **ENTRY**.

---

## COMMON blocks

A named COMMON block becomes one struct; every routine that uses it
receives it as a reference parameter and rebinds the members by name, so
the body still reads with the original names.

```fortran
subroutine init()
  common /state/ x, y
  real x, y
  x = 1.0
  y = 2.0
end subroutine
subroutine show()
  common /state/ x, y
  real x, y
  print *, x + y
end subroutine
```

```cpp
struct StateCommon {
  float x{};
  float y{};
};

void init(StateCommon& state_common) {
  auto& x = state_common.x;
  auto& y = state_common.y;
  x = 1.0f;
  y = 2.0f;
}

void show(StateCommon& state_common) {
  auto& x = state_common.x;
  auto& y = state_common.y;
  std::cout << x + y << '\n';
}

void p() {
  StateCommon state_common = {};
  init(state_common);
  show(state_common);
}
```

**Design — threaded struct vs. a global.** A COMMON block is global,
mutable, shared state. Two mappings are possible:

| Approach | Pro | Con |
|---|---|---|
| a global `StateCommon` object | shortest; closest to the Fortran | not thread-safe; hides the data flow; two concurrent "programs" share one block |
| caller-owned struct threaded as a parameter | thread-safe; data flow is explicit; the top-level caller owns the instance | verbose signatures; the threading must propagate up every call chain |

The thread-safety goal is decisive: the block is owned as a local in the
top-level caller and passed down to every routine that (transitively)
touches it. The same machinery handles module variables and SAVE. A
routine binds and is charged only for the members *it itself* declares,
and because different routines may name or even tile the same block's
storage differently, a `CHARACTER` declaration of a slot is treated as
authoritative over an implicit numeric view elsewhere.

---

## SAVE variables

A `SAVE` local must persist across calls. It moves into a per-routine
struct threaded exactly like a COMMON block (so persistence does not
rely on a hidden `static`).

```fortran
subroutine counter()
  integer, save :: n
  n = n + 1
  print *, n
end subroutine
```

```cpp
struct CounterSave {
  std::int32_t n{};
};

void counter(CounterSave& counter_save) {
  auto& n = counter_save.n;
  n = n + 1;
  std::cout << n << '\n';
}
```

**Design — threaded struct vs. `static` local.** A `static` local would
be the literal one-liner equivalent, but it reintroduces exactly the
hidden mutable global the project forbids (and is not thread-safe). The
threaded struct keeps the persisted state owned by the caller, consistent
with COMMON and module state.

---

## DATA statements

`DATA` initializers run before the body; an array initializer becomes an
array constructor.

```fortran
integer :: t(3)
data t /10, 20, 30/
```

```cpp
fortran::Array<std::int32_t, 1> t{{3}};
t = fortran::array_of(10, 20, 30);
```

---

## Derived types

```fortran
type point
  real :: x, y
end type
type(point) :: pt
pt%x = 1.0
```

```cpp
struct Point {
  float x{};
  float y{};
};

Point pt{};
pt.x = 1.0f;
```

Component access `%` becomes `.`; the type name is camel-cased.

---

## ENTRY

An `ENTRY` declares an alternate entry point that shares the routine's
storage and starts at its own statement. The converter turns each entry
into a standalone function whose body is the tail of the unit from that
point on (the primary routine keeps the whole body).

```fortran
subroutine accumulate(x)
  real :: x
  x = x + 1.0
  entry add_ten(x)
  x = x + 10.0
end subroutine
```

```cpp
void accumulate(float& x) {
  x = x + 1.0f;
  x = x + 10.0f;   // falls through into the entry's code
}

void add_ten(float& x) {
  x = x + 10.0f;   // the tail, on its own
}
```

**Design — tail duplication vs. one function with an entry selector.** A
single function taking a hidden "which entry" argument and jumping to the
right start would avoid duplicating code, but each entry has its own
argument list, which does not fit one fixed signature, and the jump
would have to weave through the `goto`-dispatch machinery. Emitting one
function per entry (each carrying its own dummy arguments) composes
cleanly with everything else — state threading and goto-structuring just
see ordinary routines. The cost is duplicated tail code and that SAVE
state shared *across* entries of one unit is modeled per-routine rather
than unit-wide.

The primary keeps the whole body because an entry's code is often reached
by falling through (or a `GOTO` into shared code, as in `FELDG`). The one
exception: when the primary's own section ends in an unconditional
`RETURN` immediately before the first `ENTRY`, that following code is
unreachable from the primary and belongs only to the entries — the
`ENCHAR` / `DECHAR` pattern (one encodes, the entry decodes). Keeping it
would make a parameter the primary only reads (`ENCHAR`'s `number`) look
*written* by the dead `DECHAR` assignment, so intent inference would
leave it mutable and a `const`/literal actual couldn't bind. In that case
the primary is truncated at the first entry.

**Function entries.** When the unit is a *function*, every entry has its
own result variable named after it, and the variables share storage, so
an assignment to one entry's name can appear in code that belongs to
another (or to the primary):

```fortran
real function area(r)
  real :: r, pi
  pi = 3.14159
  area = pi * r * r
  return
entry circum(r)
  circum = 2.0 * pi * r
end function
```

```cpp
float area(const float& r) {
  float area_result{};
  float circum{};            // sibling entry's result — a plain local here
  float pi{};
  pi = 3.14159f;
  area_result = pi * r * r;
  return area_result;
  circum = 2.0f * pi * r;    // the entry's tail — dead code after the return
  return area_result;
}

float circum(const float& r) {
  float circum_result{};
  float area{};              // the primary's result — a plain local here
  float pi{};
  circum_result = 2.0f * pi * r;
  return circum_result;
}
```

Each function lifts *its own* name into the return value (`<name>_result`)
and declares every other entry/primary result name as an ordinary local,
so an assignment like `circum = ...` reached from `area` writes a variable
rather than (illegally) the global function. (Note the per-entry
limitation: setup that runs *before* an entry — here `pi = 3.14159` — is
not replayed when that entry is called directly, matching the
tail-duplication model.)

**Dummy procedures shared across entries.** A dummy *procedure* belongs to
one entry's argument list but, through tail duplication, its call can land
in a sibling entry's body — after a `RETURN`, so the call is dead, yet it
must still type-check. The sibling doesn't receive the procedure as an
argument, so naming it would be undeclared. Each such entry declares the
procedure as an empty `std::function` local for the dead call:

```fortran
      entry first(b, out)
      out = b + 1.0
      return
      entry second(cmp, c, out)   ! cmp is a LOGICAL dummy procedure
      if (cmp(c)) out = -c
      return
```

```cpp
void first(const float& b, float& out) {
  std::function<bool(float)> cmp{};   // sibling entry's proc dummy
  out = b + 1.0f;
  return;
  if (cmp(c)) { out = -c; }           // dead, but type-checks
}
template <class F0>
void second(const F0& cmp, const float& c, float& out) {
  if (cmp(c)) { out = -c; }
}
```

`second` takes its real `cmp` as a deduced template parameter (see **Dummy
procedures**).  But `first` only references `cmp` in dead, RETURN-guarded
code carried over from `second`; a *local* can't be a template parameter,
so it falls back to an empty `std::function` of the locally observed shape.
An `EXTERNAL` result-type declaration (`logical cmp`) would normally be
dropped (it shadows the real procedure), but when the name is a dummy of a
sibling entry it is instead re-typed as the empty `std::function` local. A
plain `EXTERNAL` *called directly* — not an entry dummy — is still dropped
so the call resolves to the real global.

---

## Assumed-size array dummies

`a(*)` is the classic FORTRAN 77 assumed-size dummy: the caller fixes the
extent. It becomes a rank-1 `ArrayRef` (caller-sized), exactly like an
adjustable-bound dummy.

```fortran
      subroutine sumit(a, n, s)
      real a(*), s
      integer n, i
      s = 0.0
      do 10 i = 1, n
10    s = s + a(i)
      end
```

```cpp
void sumit(fortran::ArrayRef<float, 1> a, const std::int32_t& n, float& s) {
  s = 0.0f;
  for (i = 1; i <= n; ++i) {
    s = s + a(i);
  }
}
```

`a(m,*)` (a leading explicit dimension plus a trailing assumed one) maps
to the corresponding higher-rank `ArrayRef`.

---

## Sequence and storage association

Fortran lets an actual argument associate with a dummy of a *different*
shape as long as the storage lines up (column-major). The converter
handles this with implicit conversions in the runtime, so the call simply
type-checks; no copy is made.

```fortran
real :: m(3,4)
call work(m, 12)       ! whole 2-D array -> work's  real v(*)
```

```cpp
fortran::Array<float, 2> m{{3, 4}};
work(m, 12);           // Array<float,2> -> ArrayRef<float,1> (flat view)
```

The same covers a **scalar** actual passed to an array dummy of any rank
(it is that dummy's sole element, every extent 1), a higher-rank **view**
passed to a rank-1 dummy, and a **const / rvalue** scalar (an intent(in)
value, a literal, or an expression like `count(type) + j`) — the latter
binds for the duration of the call, like the scalar copy-in elsewhere:

```fortran
call dasadi(handle, 1, dir)       ! dir is a scalar; data dummy is  integer(*)
call dasadi(handle, 1, count(i)+j)! an expression as the array actual
```

```cpp
dasadi(handle, 1, dir);           // const int& -> ArrayRef<int,1> (1-elem view)
dasadi(handle, 1, count(i) + j);  // rvalue -> ArrayRef<int,1>
```

The reverse direction — a whole **array** actual passed to a **scalar**
dummy — also associates: the dummy is the array's first element. Here a
runtime conversion is not enough (an `ArrayRef` does not implicitly become
a `T&`), so the call site is rewritten to take the first element
explicitly:

```fortran
      i = touchi(dladsc)         ! dladsc is integer(*); touchi(n) is scalar
```

```cpp
i = touchi(fortran::first(dladsc));  // *dladsc.data() -- the first element
```

`fortran::first` returns a reference to the column-major origin
(`*a.data()`), so it works for both owning `Array` and `ArrayRef` and
preserves const-ness. (When the receiving routine then re-passes that
scalar to an *array* dummy of its own — the SPICE "counter array" idiom —
the scalar→array element-view constructor takes over; only the first
element is visible, which matches the storage the scalar was bound to.)

**Design.** A rank-changing implicit conversion is normally a smell, but
flang has already validated the association, so the converter only emits
conversions Fortran sanctioned. Doing it in the runtime (a flatten-to-1-D
`ArrayRef` constructor, plus scalar→array element-view constructors) keeps
every call site unchanged and copy-free, versus rewriting each call to
insert an explicit reshape. The whole-array→scalar direction is the one
case that must be rewritten at the call site, since no implicit conversion
from a view to a scalar reference exists (nor should it).

### Mismatched *types* (the implicit-interface pun)

Without an interface block FORTRAN 77 also lets the actual and dummy
*types* differ; the dummy then aliases the actual's storage rather than
receiving a converted value. The classic case is `MOVED`, which copies a
double-precision array by calling the integer copier `MOVEI` over twice as
many elements, and routines that stash an integer id in a `DOUBLE
PRECISION` local:

```fortran
      call movei(arrfrm, 2*ndim, arrto)   ! arrfrm/arrto are double precision
      call zzbods2c(..., instid, found)   ! instid is double; code dummy is integer
```

```cpp
movei(fortran::reinterpret_array<std::int32_t>(arrfrm), 2 * ndim,
      fortran::reinterpret_array<std::int32_t>(arrto));
zzbods2c(..., fortran::storage_ref<std::int32_t>(instid), found);
```

A C++ reference or `ArrayRef` view cannot bind a value of a different type,
so the call site reinterprets the storage: `fortran::storage_ref<To>` for a
scalar (`*reinterpret_cast<To*>(&x)`) and `fortran::reinterpret_array<To>`
for a rank-1 array (a flat view of the same bytes, the element count
rescaled by the size ratio). This matches Fortran's by-reference aliasing.

**Design.** The rewrite is gated on the *would-otherwise-fail* cases only:
a non-`const` reference dummy (intent out/inout) or an array-view dummy
whose element type differs — neither has a binding. A `const`-ref or
by-value dummy already converts implicitly (a genuine value conversion, as
intended for ordinary numeric promotion), so it is left untouched. The gate
guarantees no currently-compiling call site changes behavior; only calls
that previously failed to compile gain the reinterpretation. (A read-only
mismatched scalar dummy is the one residual gap: it value-converts silently
rather than punning, which is correct for ordinary promotion but not for a
deliberate same-storage pun — rare, and indistinguishable without an
interface block.)

---

## Assumed-length CHARACTER dummies

A `CHARACTER*(*)` dummy has a caller-determined length. It becomes a
`fortran::CharRef` — a non-owning character view (the string analog of
`ArrayRef`):

```fortran
      subroutine ucase(in, out)
      character*(*) in, out
      out = in
      end
```

```cpp
void ucase(fortran::CharRef in, fortran::CharRef out) {
  out = in;              // copies characters into the caller's storage
}
```

A `CharRef` reads as a `std::string_view`, assigns with Fortran
blank-pad/truncate semantics, supports substring indexing `out(lo, hi)`
(an open upper bound `s(lo:)` lowers to `s(lo, fortran::len(s))`, which
works for a `CharRef` or a `FortranString`), blank-padded comparison
(`==` / `!=`), and list-directed `>>` reads. It is constructible from a
mutable or `const`
`FortranString`, a `string_view`, a `std::string`, a literal, or a
substring proxy (`s(i:j)` passed as an actual), so any character actual
binds. An assumed-length CHARACTER *local* — which only arises for an
ENTRY-shared dummy that isn't the current entry's argument — is likewise
emitted as a (null-initialized) `CharRef` rather than a `std::string_view`
value, so `s(i, j)` still type-checks there.

**Design — `CharRef` vs. `std::string_view&` vs. `std::string`.** A
mutable `std::string_view&` was the first attempt but is wrong twice over:
it can't bind a non-lvalue actual (a literal or a concatenation), and a
`string_view` is read-only so the callee can't write characters back.
`std::string` would own/resize, mismodeling fixed-length semantics.
`CharRef` is the minimal "writable view of N caller-owned characters", and
using it for **read-only** assumed-length dummies too (not just writable
ones) means substrings `s(lo:hi)` work uniformly — `std::string_view` has
no `operator()(lo, hi)`.

The reverse association also occurs: a `CHARACTER*(*)` actual (a `CharRef`)
passed to a *fixed*-length `CHARACTER*N` dummy. `FortranString<N>` is
constructible from a `CharRef`, copying the first `N` characters
(blank-padded), so the call binds:

```fortran
      if ( eqchr(type, 'C') ) ...     ! type is CHARACTER*(*); eqchr's dummies are CHARACTER*1
```

```cpp
if (eqchr(type, "C"sv)) ...           // CharRef -> FortranString<1> (first character)
```

---

## Assumed-length CHARACTER arrays (character cells)

An assumed-length character *array* dummy — `CHARACTER*(*) cell(*)`, the
SPICE "character cell" — can't be an `ArrayRef<std::string_view>`: the
element length is a runtime value, not a C++ type. It becomes a
`fortran::CharArrayRef`, whose indexing yields a `CharRef`:

```fortran
      subroutine first(cell, item)
      character*(*) cell(*), item
      cell(1) = item
      end
```

```cpp
void first(fortran::CharArrayRef cell, fortran::CharRef item) {
  cell(1) = item;        // cell(1) is a CharRef -> writes element 1
}
```

`CharArrayRef` carries a base pointer, the element length, the lower
bound, and the count; `cell(i)` returns `CharRef(base + (i-lo)*len, len)`.
It is constructible from a fixed-length character array
(`Array<FortranString<N>, 1>`) or a single character scalar.

**As an ENTRY-shared local.** An assumed-length array can only ever be a
dummy argument. But ENTRY tail-duplication (see *ENTRY*) copies the whole
shared body into each entry, so a cell that is a dummy of *one* entry can
be referenced in code duplicated into a *sibling* entry that does not
declare it. There it is not a parameter, so it would be emitted as an
(impossible) `Array<std::string_view, 1>` local. Instead it becomes a
null `fortran::CharArrayRef` — the referencing code is unreachable for
that entry (the entry returns first), so the view is never dereferenced;
it only has to compile and bind to the `CharArrayRef` callees it is
forwarded to.

---

## Names that shadow a library routine

A routine may have a local (or COMMON / SAVE) array whose name collides
with a *different* global subprogram — e.g. a routine's local pool array
`STPOOL(*,*)` versus the library's `STPOOL` function. In that routine,
`stpool(i, j)` is array indexing, not a call.

```fortran
      common /pool/ stpool(2, mxpool)   ! a local pool array named STPOOL
      ...
      savep = stpool(forwrd, p)         ! indexing, even though a global
                                        ! function STPOOL also exists
```

```cpp
savep = stpool(forwrd, p);             // indexing the bound common array
// NOT: stpool(state..., forwrd, p)    // would be a call to the library fn
```

**Design.** The lowering and state-plumbing passes treat a `name(...)` as
a call only when `name` is a known subprogram **and** is not a local /
dummy / common-bound name of the current routine. A data name shadows a
like-named global in its own scope (Fortran's rule), so it never receives
threaded state arguments.

---

## INCLUDE files

A Fortran `INCLUDE 'foo.inc'` is expanded by flang during parsing (it
resolves the path relative to the including file), so the converter sees
the included declarations inline and needs no special handling — the
`PARAMETER`s and COMMON layouts in a shared `.inc` flow through exactly as
if written in place.

---

## Command-line and environment intrinsics

The toolkit's command-line programs reach the process environment through
de-facto-standard vendor intrinsics: `IARGC` / `NARGS` / `GETARG` (command
arguments), `GETENVQQ` (environment variables), and `SYSTEMQQ` / `SYSTEM`
(run a shell command). These map to `fortran::` runtime helpers.

```fortran
      n = iargc()
      call getarg(1, arg, status)
```

```cpp
n = fortran::iargc();
fortran::getarg(1, arg, status);
```

The command line is genuinely process-global, set once at start-up and
only read afterward, so the generated `main` captures it and the runtime
serves it — the one place a process-wide store is the right model rather
than threaded state:

```cpp
int main(int argc, char** argv) {
  fortran::set_command_args(argc, argv);
  shellmain();
  return 0;
}
```

`IARGC()` returns the user-argument count (excluding the program name);
`NARGS()` is the Compaq form that includes it. `GETARG(k, value, status)`
copies the k-th argument (k = 0 is the program name) into `value` with
Fortran blank-pad/truncate; `status` receives the length, or -1 if absent.

**I/O unit expressions.** A unit need not be a literal or a bare variable
— `READ (UNITS(NEST), …)` uses an array element. The unit is lowered as a
full expression, so the subscript is preserved (`_units.in(units(nest))`)
rather than collapsing to the bare array name.
