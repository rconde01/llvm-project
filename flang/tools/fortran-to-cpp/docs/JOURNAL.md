# Converter Journal — problems encountered and how we fixed them

A high-level, chronological-by-theme record of the substantive problems hit
while building the Fortran→C++ converter, and the fix for each.  It
complements the per-construct catalog in [`CONSTRUCTS.md`](CONSTRUCTS.md)
(what each construct maps to) and the design contract in
[`../README.md`](../README.md) (the rules the output must obey).  For exact
diffs, see the git log — commit hashes are cited inline.

The throughline: **prefer flang's resolved facts over re-derived
heuristics** (extend the JSON dumper rather than guess in Python), and keep
the output **readable and thread-safe** (no mutable globals; every piece of
state lives in an object the caller owns).

---

## 1. Foundation — getting facts out of flang

**Problem.** A source-to-source translator needs semantics (resolved
types, symbol attributes, storage layout), not just syntax. Re-deriving
them in Python is fragile.

**Fix.** We consume flang's analyzed parse tree as JSON
(`-fdebug-dump-analyzed-tree-json`) and built a typed Python loader
(`flang-ast-py`). The dumper was extended in waves to emit exactly the
facts the converter needs rather than have it guess:

- defining source, COMMON/EQUIVALENCE layout, procedure interfaces,
  implicit typing, non-integer constants (`14f2d4bdd`)
- storage size/offset, `BIND(C)` name, USE source (`e9c42f6a8`)
- procedure signatures, derived-type components, generics, USE rename
  (`15bd8ec17`)
- type-bound bindings, NAMELIST, ASSOCIATE, initializers (`e523751fd`)
- per-`Name`/`Expr` semantic facts: type, rank, **category**, value —
  later used to replace IR-shape pattern-matching with the authoritative
  AST `category` (`03ea1f40d`, `6a18b4dea`); surfaced onto `IRLocal`
  (`ac9a94867`)
- all symbol **attrs** (INTENT, OPTIONAL, SAVE, POINTER, …) flow through
  `name.attrs`, so attribute statements are handled uniformly
  (`b4e500c2b`)

**Lesson that recurs below:** every time a heuristic got the wrong answer,
the durable fix was a new dumper field, not a smarter guess.

---

## 2. Arrays, bounds, and Fortran's storage model

This was the largest and subtlest area — Fortran's array model (arbitrary
lower bounds, column-major, sequence/storage association, assumed-size) does
not map onto C++ containers directly.

**Configurable lower bounds.** Fortran arrays can start at any index
(`A(0:9)`, `CELL(-5:*)`). Storing a runtime lower bound and subtracting it
on every access is both slow and loses constant-folding.
*Fix:* `Array`/`ArrayRef` carry the per-dimension lower bound as a
**non-type template parameter** (`std::array<index_t,Rank>`), so a literal
bound constant-folds into indexing while a runtime sentinel still supports
dynamic bounds (`23656e233`, `af7fdb0c8`, `2c10f67b2`, `f150053da`). Named
PARAMETER lower bounds (e.g. SPICE's `LBCELL = -5`) are folded to the static
literal so cells get a compile-time bound (`deb3a8997`).

**Explicit-shape dummies must be 1-based.** A Fortran explicit-shape dummy
`V(3)` is always 1-based regardless of the actual's bounds. We were letting
the actual's bounds leak through, which broke quaternion/rotation code.
*Fix:* explicit-shape array dummies are emitted with a static `{1}` lower
bound (`c3b798e1d`). This type-level fix replaced an earlier, abandoned
attempt to *rebase at the call site* (`190f8829e`, `63e82092f`, both
reverted) — instrumentation proved call-site rebasing changed the runtime
lower bound globally and corrupted unrelated routines; the static-NTTP
approach changes nothing at runtime.

**Assumed-size dummies `A(*)`.** The trailing dimension has no extent; the
callee indexes into the actual's storage.
*Fix:* keep the leading extents, size the trailing dim from the actual,
and carry the dummy's declared lower bounds (`921f50cbf`, `cab017f6b`,
`543f06f17`, `0dbace530`).

**Sequence association — array actual → scalar/lower-rank dummy.** F77 lets
you pass `A(6)` to a `V(2,3)` dummy, or a whole array to a scalar dummy
(which then sees the first element).
*Fix:* a whole-program reshape pass (`_reshape_sequence_associated_args`)
inserts the right view/`ftn::first` at the call site (`c7d63c5ce`,
`bd2190e5a`, `e8bda14f1`).

**Sequence association — scalar actual → array dummy (this session).** The
mirror case bit us last: SPICE passes a counter array's first element to a
scalar dummy (`zzscup01`'s `POLCTR`), which forwards it to an *array* dummy
(`zzpctrck` → `zzctrchk` reads `CTR(2)`). The scalar→`ArrayRef` view had
extent 1, so the debug bounds check rejected the valid, caller-provided
second element — crashing 35 tspice families once kernels loaded.
*Fix:* a scalar bound to an array dummy gets an **assumed-size extent**
(`kAssumedExtent`), so the callee indexes into the caller's real storage;
genuine out-of-bounds on a true array still throws (`fd163d92f`). The
sentinel had to be excluded from `total_size`, or `do i=1,size(dummy)`
loops ran ~10^12 times (regressed `f_gfpa` to a hang) (`813ed126e`).

**Storage association across mismatched types.** An implicit interface lets
a `DOUBLE PRECISION` array bind an integer-copy routine, or a `double` be
used as an `int*`. A C++ reference can't bind a different type.
*Fix:* `ftn::storage_ref<To>` / `ftn::reinterpret_array<To>` inserted only
where the binding would otherwise fail to compile (`docs/CONSTRUCTS.md`
§"Mismatched types").

**COMMON and EQUIVALENCE.** COMMON blocks are byte-offset layouts that
different routines carve up differently (a `REAL` array here, an `INTEGER`
scalar at the same offset there); EQUIVALENCE aliases storage.
*Fix:* byte-offset COMMON layout with finer-grained sub-views bound by
reference (`08c4c5821`, `682cbb71e`), member-reuse disambiguation
(`9e2106f62`), `LOGICAL` sized 4 bytes to match layout (`fa0872d25`),
COMMON array reshape across declarations (`c7d63c5ce`), and EQUIVALENCE
element aliases integrated with passing / byte I/O / seq-assoc
(`c85a7157e`, `21cb7e6db`).

---

## 3. Characters

Fortran CHARACTER is fixed-length, blank-padded, with 1-based inclusive
substrings and a CHARACTER↔INTEGER type pun in COMMON.

**Representation.** `FortranString<N>` (owning, blank-padded) for
fixed-length; `CharRef` (non-owning writable view) for assumed-length
dummies `CHARACTER*(*)`; substring proxies that read/write through to the
backing store.

**Substring/cell self-assignment no-op.** `s(i:j) = s(k:l)` and
equivalence-cell-to-itself assignments silently did nothing — the proxy's
implicit copy-assign copied the *view* (pointer+size), not the characters.
*Fix:* explicit character-copying assignment on the proxies (`01813e188`).
(This one had slipped past the compile-only corpus gate because no unit
test did proxy-to-proxy assignment — it motivated more run-tests.)

**Assumed-length function results (this session).** A `CHARACTER*(*)`
*function result* was typed like an assumed-length *dummy*: a non-owning
view with no backing. So the function wrote its value into nothing and
returned empty — silently breaking every assumed-length character function.
The worst instance was the test-utility `BEGDAT()`, which returns the
`\begindata` marker; with it empty, no kernel text ever loaded.
*Fix:* an owning, dynamic `ftn::DynString` returned by value; it carries
the `fortran_char_view_proxy` marker so it still binds to `FortranString`
slots and `CharRef` dummies (`184aaa906`).

**CHARACTER↔INTEGER pun, A-descriptor null views.** Bit-reinterpret a
`FortranString<N>` as an integer of matching width for the classic COMMON
type pun; guard list-directed `A` output against a null character view so a
default-constructed cell prints blanks instead of crashing (`644998559`).

---

## 4. I/O

Fortran I/O is a large surface: unit tables, FORMAT edit descriptors,
list-directed vs formatted, direct-access records, internal files.

**Unit table, thread-safe.** No global unit table; a `ftn::io::Units`
object is threaded as a reference parameter into every routine that does
unit I/O (`b0c56707e`), including INQUIRE/BACKSPACE/REWIND-only routines.
Unattached units lazily open `fort.<N>` (`53c542ec4`).

**FORMAT interpreter.** Fixed-width sequential reads slice columns rather
than `>>`-tokenize (needed for column-packed files like `apf107.dat`)
(`e5b4eccff`); a runtime FORMAT interpreter handles non-constant implied-do
bounds and format cycling (`4e1db096f`, `51e7ff4d6`, `12c6c442e`); WRITE
honors FORMAT with implied-do/whole-array items (`29ec84711`); `$` and `1X`
record control honored (`64a760b9e`, `b52effba6`).

**List-directed quirks.** Fortran separators (comma, tab, `/`) drive
tokenizing; a `/` terminator (and EOF) leaves remaining items at their
**current value** (C++ `>>` would zero them).
*Fix:* a filtering input streambuf rewrites separators, and
`read_list_item` saves/restores the target on failure (`89b7b26e1`,
`f0f1e2af4`, `b52effba6`). D-format exponents (`2.71D+02`) are translated to
`E` so `>>` parses them (`1776a031f`).

**Whole-line `A` reads corrupting character data (this session).** Two
linked bugs blocked all kernel text loading:
1. `READ(unit,'(A)') line` was lowered to a list-directed token read, which
   stops at the first blank.
2. The list-directed input streambuf (above) also rewrites `D`/`d`→`E`/`e`
   and comma/tab→space — and `getline` read through that same filter, so a
   whole-line read of character data was silently corrupted (`\begindata`
   came back as `\begineata`, so the kernel data-section marker never
   matched).
*Fix:* a bare-`A` format now reads the whole record via `getline` through a
new **unfiltered** `Units::in_raw()` (`0c670ab44`). This single fix
unblocked the entire kernel/time/SPK/CK family swath.

**Direct-access, unformatted, scratch.** Record-based formatted/unformatted
direct I/O (`b2638de62`, `f052bb7c4`, `9cb4c08c6`); `STATUS='UNKNOWN'`
creates a new file instead of dropping writes (`72e3579c4`); `STATUS=
'SCRATCH'` gets a real temp backing file and REWIND flushes before seeking
(`db6003c23`).

---

## 5. State, control flow, and higher-order routines

**Thread-safe state (the central design choice).** No `static` locals, no
mutable globals. The state-plumbing pass builds per-routine structs for
COMMON, SAVE, MODULE data, and workspace (fixed-size local arrays) and
threads them as reference parameters through the call graph
(`state_plumbing.py`). SAVE-struct array bounds that reference a routine-
local PARAMETER hoist that constant as a `static constexpr` struct member
(`c2a7918c0`).

**SAVE/DATA initialization timing.** A SAVE variable's DATA initializer
must run **once**, not per call; several silent drops of DATA initializers
were fixed (`1455d2d19`, `67d23a727`, `ecb33ad5a`, `f052bb7c4`,
`5317ffd34`).

**BLOCK DATA.** Lowered to a load-time COMMON initializer, with calls
inserted by the whole-program pass (`51508617a`, `a752968a4`).

**ENTRY points.** A subprogram with ENTRY points becomes several functions
sharing the umbrella's SAVE state; dead post-`RETURN` code in a sibling
entry still has to type-check (empty `std::function` locals for entry-shared
dummy procedures) (`7973025f4`, `CONSTRUCTS.md` §ENTRY).

**Dummy procedures as templates.** Rather than infer cross-program
procedure signatures, any routine taking a procedure dummy is emitted as a
function **template**, and every procedure actual is wrapped in a
state-capturing generic lambda — the compiler deduces everything
(`934718dca`). Callback args pass by reference, not `std::forward`
(`183ca2e5b`).

**GOTO / control flow.** Arbitrary GOTOs are structured into
loops/conditionals with a computed-goto dispatcher fallback.

---

## 6. Robustness and process

**Fail loudly.** Early on, unhandled constructs could silently drop code
(e.g. DATA initializers). We switched to failing loudly on unhandled
constructs so gaps surface at convert time rather than as wrong runtime
behavior (`6c81d003b`, `7973025f4`).

**`corpus_fixes/`.** A handful of corpus files use non-conforming source
flang rejects; standard-conforming patches live in `corpus_fixes/`,
substituted in during measurement so the SPICE corpus compiles **1625
files, 0 errors** end-to-end (`e846a2dfe`, `11107bd23`).

**Validation gates.** Every change is held to: the converter unit suite
(~407 tests, compile-and-run where it matters), the corpus chunk-compile
(1625 files / 0 errors), and — for runtime-affecting changes — the tspice
behavioral suite. The substring no-op bug taught us that a compile-only
gate is not enough; runtime tests guard semantics.

---

## 7. The tspice debugging arc (this session)

Bringing up the SPICE test harness (`tspice`) end-to-end exposed a chain of
bugs whose fixes are described above; the order of discovery is itself
instructive, because each fix uncovered the next layer:

1. **CRASH cluster** (caught throws) — assorted array-extent and I/O bugs;
   fixing the explicit-shape-dummy and SCRATCH/UNKNOWN-file issues cleared
   most.
2. **Kernel loading FAIL swath** — traced six layers down (pool store →
   file read → marker detection) to two root causes: `BEGDAT()` returning
   empty (assumed-length result bug, §3) and `\begindata` corruption in
   whole-line reads (§4). Fixing both took PASS from ~200 to ~257.
3. **A new 35-family CRASH cluster** appeared *because* kernels now loaded
   and routines ran further — all one bug: the scalar→array counter
   sequence association (§2). Fixing it (and the `size()` follow-up) took
   CRASH from 35 to 2 and PASS toward ~269.

Net so far this session: **PASS 200 → ~269, CRASH 12 → 2**, with the SPICE
corpus held at 1625 files / 0 errors throughout.

**Method that worked repeatedly:** when a family failed, instrument the
suspected routine with `fprintf` probes (recompile that one file + relink),
read the actual values, and only then fix — and when a fix worked,
generalize it and sweep the codebase for sibling cases rather than patching
the single call site.
