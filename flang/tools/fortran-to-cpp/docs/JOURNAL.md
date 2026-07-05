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
sentinel must be excluded from `total_size`, or a `do i=1,size(dummy)`
loop over such a view would iterate ~10^12 times — a defensive follow-up so
the huge extent can never leak into a loop bound (`813ed126e`).

**Storage association across mismatched types.** An implicit interface lets
a `DOUBLE PRECISION` array bind an integer-copy routine, or a `double` be
used as an `int*`. A C++ reference can't bind a different type.
*Fix:* `ftn::storage_ref<To>` / `ftn::reinterpret_array<To>` inserted where
the binding would otherwise fail to compile (`docs/CONSTRUCTS.md`
§"Mismatched types").  Extended this session to intent(in) scalar actuals:
the pun was gated to non-const dummies (a const-ref "converts implicitly"),
but that conversion converts the *number*, not the *bytes* — wrong for
storage association.  GETFVN declares `DOUBLE PRECISION INSTID`, lets
ZZBODS2C write an INTEGER body code into its bytes, then passes it to
GETFOV's INTEGER dummy; the intent(in) call did a double→int value
conversion, so the integer bit pattern read as a denormal ~0 and GETFOV
looked up `INS0_FOV_FRAME` — failing the FOV cluster.  Now an intent(in)
scalar is punned too, but only when the dummy's type fits within the
actual's storage (a 4-byte INTEGER reading an 8-byte DOUBLE — the callee
never reads past the actual).

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

**`array_of` of character views built blank tables (this session).** A
Fortran character array constructor whose elements are `CHARACTER`
PARAMETER constants — `[RECSYS, LATSYS, ...]` — lowered to
`ftn::array_of(recsys, ...)`.  `array_of` deduced its element type as the
common type of its args; for character *views* (`CharRef`) that's the view
itself, so it built an `Array<CharRef>` of null views and assigned
`r(i) = elem`, which (per CharRef's character-copying `operator=`) wrote
characters *through* the null view — a no-op.  The table came back blank.
SPICE's ZZGFCOIN builds its coordinate-name tables this way and looks them
up with ISRCHC; blank tables made every coordinate system read as "not
supported", failing the whole geometry-finder (GF) cluster.  *Fix:* when
the element type is a character view, `array_of` builds an owning
`Array<DynString>` instead — recovered ~35 families (`c2ec9b2bc`).

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

**CLOSE(STATUS='DELETE') never removed the file (this session).**
`_lower_close` dropped every CLOSE specifier and always emitted a bare
`_units.close(unit)`, which only disconnects the unit — so a
`CLOSE(u, STATUS='DELETE')` left the file on disk.  The DDH file-kill path
(ZZDDHMAN/ZZDDHCLS with `KILL=.TRUE.`) closed but never deleted, so a later
`INQUIRE(EXIST=)` still saw the file (f_ddhcls).  *Fix:* `_lower_close`
reads the STATUS= specifier and passes it through; a new runtime
`close(unit, status)` overload removes the backing file when the status is
'DELETE' (evaluated at run time, so a variable status works too).

**DAF/DAS read-modify-write losing records (this session).** A
``FortranFile``'s input and output sides are one shared ``std::fstream``.
The DAF/DAS layer reads a record (even a brand-new one, past EOF), updates
it, and writes it back.  Reading past EOF sets eofbit/failbit on the shared
stream; the record writers then ran ``seekp``/``write`` *without clearing*
that state, so on a failed stream both were silent no-ops — every record
written after such a read was lost.  A freshly built SPK/CK/DAS/EK file
ended up missing its data records (a type-2 SPK segment's data at record 5
of a 4-record file), so reads returned zeros and the address bookkeeping
later tripped ``SPICE(DAFBEGGTEND)``.  *Fix:* ``clear()`` before the
seek/write, exactly as the reader already did — recovered ~40 families in
one change.

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
   CRASH from 35 to 2 and PASS toward ~276.
4. **The SPK/CK FAIL cluster** then traced to the DAF read-modify-write
   record-loss bug (§4) — one ``clear()`` recovered ~40 families.
5. **The geometry-finder (GF) FAIL cluster** traced to `array_of` of
   character views building blank coordinate tables (§3) — recovered ~35.
6. **The FOV cluster** (getfov / zzbods2c) traced to a `DOUBLE
   PRECISION`-holding-an-INTEGER storage-association actual passed to an
   intent(in) INTEGER dummy without a byte reinterpret (§2).
7. **f_ddhcls** traced to `CLOSE(STATUS='DELETE')` never removing the file
   (§4).
8. **f_slice** traced to a mixed nested implied-DO DATA statement
   (`((SMPN(J,I),J=1,3),SMPC(I),I=1,N)`) whose interleaved second array was
   silently dropped and mis-filled the first (§2, DATA lowering).

Net this session, on the **original 120 s / 6-worker harness** (the same one
that produced the 200 baseline, so like-for-like): **PASS 200 → 345, FAIL
127 → 4, CRASH 12 → 1, TIMEOUT 24 → 15, HARD 2 → 0, 0 regressions**.  With a
300 s timeout on an idle box the count is **358** — the difference is
slow-but-correct GF / DSK / illumination families that exceed 120 s under
load, not correctness failures.  Throughout, the SPICE corpus held at 1625
files / 0 errors and the converter suite stayed green (409 tests).

The recurring shape across all these runtime bugs: a **silent failure** —
an empty return, a lost write, an over-strict bounds check, a blank table —
that stayed hidden until an earlier fix let execution reach it. Each fix
uncovered the next, so the PASS count moved in large steps
(200 → ~257 → ~276 → 316 → 351 → 357) rather than one at a time.

**Remaining non-PASS (all individual, no shared cluster):**
- `f_gftfov`, `f_zzdskbsr` — not broken, just slow: a heavy GF+DSK
  ray/search that runs ~5 min (f_zzdskbsr passes at 309 s alone;
  f_gftfov exceeds 340 s).  A translation-speed gap vs Fortran, not a
  correctness bug.  `f_subpnt` only times out under measurement-load
  contention (13 s alone).
- `f_ek02` (was CRASH, now **PASS**) — a debug-build bounds trip in the EK
  type-04/05 / DAS write path, from an `ArrayRef` extent collapsing down a
  chain of assumed-size `(*)` dummies (`ekuced → zzekue05 → zzekad05 →
  dasudd → dasurd → MOVED/MOVEI`).  `MOVEI`'s dummies are `ARRFRM(*)` /
  `ARRTO(*)`, so `MOVEI(.., N, ..)` indexes to `N` while the received view
  tracked fewer elements → trip at `index 3 of [1,2]`.
  **Fix (the broad assumed-size fix):** an assumed-size `(*)` / `(M,*)`
  dummy has no Fortran upper-bound check on its last dimension, so at callee
  entry the emitter now widens the received view's last extent to the
  unbounded `kAssumedExtent` in place — `ftn::assume_size(p);` (a statement,
  because `ArrayRef::operator=` is Fortran element assignment, not a rebind;
  the widening is a new in-place `ArrayRef::set_assumed_last_extent`).  This
  only *removes* upper-bound checks (matching Fortran), so it cannot turn a
  passing run into a crash.  Validated: corpus 1625/0, 411 converter tests,
  and tspice CRASH count 1→0 with no new FAIL (the heavy geometry families'
  TIMEOUT count varies only with measurement load — each passes run alone).

**Reproducible builds & clean-load measurement (session).**  Two build
hazards were fixed so the tspice tally is trustworthy: (a) state-parameter
order was derived from `set` iteration (hash-seed dependent), so artifacts
from different converter runs could not link — now `sorted()` (see the
state-plumbing fix).  (b) A tally is only valid under low load: running two
`run_families.py` instances at once drove the 4-core box to load ~22 and
spuriously reclassified compute-heavy families as TIMEOUT/HARD (a bad
`PASS 292` reading); the same binary under normal load gives the real
`PASS 348, TIMEOUT 13, FAIL 3, CRASH 1`.  The 13 TIMEOUTs are the known
slow GF/DSK/SPK/pool families (translation-speed gap), the 3 FAILs and
1 CRASH are the pre-existing known set below — i.e. no regression.
- `f_ddhopn`, `f_dla`, `f_zzasc2` (were FAIL, now **PASS**) — error-path
  tests expecting `SPICE(FILEOPENFAIL)` when an OPEN cannot succeed.  Root
  cause: the OPEN statement's `IOSTAT=` specifier was dropped during
  lowering, so the status variable stayed 0 and the failure was never seen.
  Fix: ``_lower_open`` routes `IOSTAT=var` into ``var = _units.open(...)``,
  and ``Units::open`` returns the Fortran IOSTAT while enforcing the
  pre-open existence rules (STATUS='NEW' fails if the file exists,
  STATUS='OLD' fails if it does not).  A companion ``_uses_units`` fix makes
  the units table thread when a routine's only unit use is an OPEN with
  IOSTAT (the call now appears as an expression, not a bare statement).
  This work also broadened I/O to be Fortran-consistent across modes -- OLD
  opens read+write (with a read-only fallback), POSITION='APPEND' and the
  ENDFILE statement are implemented -- pinned by a comprehensive
  ``tests/test_file_io_modes.py`` (STATUS, CLOSE STATUS, REWIND, BACKSPACE,
  ENDFILE, DIRECT+RECL, UNFORMATTED, POSITION='APPEND', INQUIRE).

**Final tspice state (clean-load):** **no CRASH, no HARD**; the only FAILs
are `f_spk01` / `f_spk21`, which now *link* (367 live families, up from 365,
as the I/O and units fixes let more routines compile) and expose a
pre-existing ill-conditioned-solver precision limit (~1.2e-9 vs a 5e-12
tolerance -- see the SPK type-01/21 section below), not a logic bug.
Everything else passes or TIMES OUT -- the compute-heavy GF/DSK/SPK/pool
families are a translation-speed gap, not a correctness bug (each passes
when run alone; the exact PASS/TIMEOUT split shifts with measurement load).
Corpus stays 1625 files / 0 errors; 431 converter tests pass.
*(f_slice is now fixed — see arc item 8.)*

**File-I/O status (all correctness families now PASS).**  After the OPEN
IOSTAT= fix the tspice tally is CRASH 0 / FAIL 0; every remaining non-PASS
family is a compute-heavy geometry/GF/DSK/SPK TIMEOUT (a translation-speed
gap -- each passes when run alone under low load), not a correctness bug.

**Method that worked repeatedly:** when a family failed, instrument the
suspected routine with `fprintf` probes (recompile that one file + relink),
read the actual values, and only then fix — and when a fix worked,
generalize it and sweep the codebase for sibling cases rather than patching
the single call site.

---

## Non-SPICE differential testing (atmospheric-model corpora)

To find converter bugs beyond SPICE, the non-SPICE example corpora were
differentially tested: build each Fortran library + driver two ways -- native
`gfortran -O0` and via the converter (`g++ -O0`) -- run both, and compare the
numeric output token-by-token (relative tol 1e-9; -O0 both sides so only libm
differs).  gfortran needs `-std=legacy -fallow-argument-mismatch
-fdec-char-conversions` to accept these legacy F77 sources.

**Results:** NRLMSISE-00 (built-in test driver, 777 numbers) and MSIS-90
(730 numbers) reproduce **bit-identically** (worst relative difference
0.0).  Two converter bugs were found and fixed along the way:

1. **Whole-array CHARACTER->INTEGER type-pun assignment.**  A numeric COMMON
   slot aliased as CHARACTER (NRLMSISE-00 `/DATIM7/` ISDATE/ISTIME/NAME)
   receives `isdate = ftn::array_of("01-F"sv, ...)` where `isdate` is
   `Array<int32_t>`.  `Array::operator=` did `static_cast<int>(string_view)`
   and failed to compile.  It now bit-reinterprets the character bytes into
   the integer (blank-padded) -- the array analogue of
   `FortranString::operator I()` (`array.hpp`).

2. **Legacy `A(1)` assumed-size dummy idiom.**  NRLMSISE-00 GLOBE7 declares
   `DIMENSION P(1)` but indexes P(1..150).  A dummy array whose trailing
   extent is literal `1` is now treated as assumed-size (`ftn::assume_size`,
   unbounded last dim), alongside `A(*)` (`emit.py`).

**Open (supervised):** MSIS-86 PRMSG5 uses a `/DATIME/` COMMON whose members
are punned as INTEGER in one routine and CHARACTER (mixed lengths) in
another; the two layouts have *different total sizes*, so a character
sub-view straddles two canonical integer fields and the `name` binding is
dropped (`state_plumbing.py:_canon_field_for_offset`).  The canonical layout
would need to model punned members as a raw byte span.  The IRI / IGRF /
radbelt / CIRA corpora need external coefficient data files that aren't
present in this environment, so they weren't differentially run.

---

## Optimized performance: converted C++ vs native Fortran

Head-to-head timing of 11 heavy tspice families, both optimized and with no
bounds checks: converted **g++ -O2 -DNDEBUG** vs native **gfortran -O2**
(gfortran needed `-std=legacy -fallow-argument-mismatch -fdec-char-conversions`
to build the whole toolkit; flang's runtime was not built in this
environment, so gfortran is the reference).  Each family run serially in a
fresh dir; both binaries pass the same self-checks.

Results were **mixed and family-dependent**: compute-bound families ran
~1.5-4.4x slower in C++ (f_subpnt 4.4x, f_xdda 3.5x, f_dyn01 2.1x -- the
state-plumbing / ArrayRef indirection overhead), while several geometry-finder
and DSK/search families ran *faster* in C++ (f_gftfov 0.10x, f_gfrr 0.16x,
f_zzdskbsr 0.41x).  Median ~1.65x slower; aggregate total 0.70x (i.e. faster
overall, skewed by the two heaviest search families).

Caveat: the GF/search families are iterative root-finders whose iteration
count is sensitive to tiny floating-point differences between the two
toolchains -- both converge to correct answers (all pass), but the wall time
reflects convergence-count differences as much as raw speed, so those ratios
are not a clean compute-speed measure.  The compute-bound families
(~1.5-4x slower) are the representative translation-overhead figure.

Note on debug builds: the same families under the default **-O0 with bounds
checks** ran 80-300 s; at -O2 -DNDEBUG they run 1-25 s.  The runtime
bounds check on every array access dominates -O0 time for index-heavy code,
so perf claims must use -DNDEBUG.

### Shrinking the ArrayRef view (the indirection overhead above)

The "ArrayRef indirection overhead" that makes compute-bound families slower
is the fat by-value view (pointer + lower + extents + strides = 32-40 bytes)
plus the runtime index math the optimizer can't fold across non-inlined
calls.  Three NTTPs progressively drop members and unlock folding, each a
``[[no_unique_address]]`` conditional store with a uniform read API so the
indexing code is identical either way:

- **#2 static lower** (``Lower`` NTTP, already present): a literal-lb dummy
  drops ``lower_`` and folds ``idx - lower``.
- **#1 contiguous** (``Contiguous`` NTTP): a F77 array dummy is always
  contiguous, so it drops ``strides_`` and derives the column-major offset
  in one pass (``linear_offset_contig``).  A strided ``section()`` stays
  ``Contiguous=false``.
- **#3 static extents** (``Extents`` NTTP): a fixed-size dummy (``V(3)``,
  ``M(3,3)``) drops ``extents_`` and constant-folds the whole offset -- the
  key -O2 win, since a runtime extent member is an opaque load the compiler
  can't fold across a non-inlined call.  Excludes extent-1 dims (the F77
  one-element-dummy assumed-size idiom, which a static ``{1}`` would wrongly
  bound).

Combined, a fully-static contiguous dummy is **just a pointer** (``sizeof``
8, down from 24/40 at rank 1/2).  The emitter tags every non-POINTER
static-lower array dummy ``Contiguous=true`` and adds static ``Extents``
when all extents are literals > 1; POINTER dummies keep the runtime form
(their target may be a non-contiguous section).  Validated compile-clean
(corpus 1625/0) and behavior-preserving (434 converter tests, incl.
compile+run of static/contiguous views and the one-element-dummy idiom).
The payoff is at -O2 (constant-folded indexing + view elimination); the
-O0 tspice tally is unaffected (bounds checks still dominate there).

### Measured -O2 payoff (after the shrink)

Isolated A/B (same Fortran 3x3 ``mxv`` kernel, fixed-size dummies, hot loop
across a TU boundary, ``g++ -O2 -DNDEBUG`` pre-refactor vs HEAD): the dummy
view drops from **56 bytes to 8** (a bare pointer) and the kernel runs
**6.15 s -> 3.15 s = 1.95x faster**, identical result -- the clean isolation
of the win.

Full compute-bound families, converted ``-O2 -DNDEBUG`` vs native
``gfortran -O2`` (both built here, same box, low load, best-of-3):

| family    | converted | gfortran | ratio (HEAD) | ratio (pre-shrink) |
|-----------|-----------|----------|--------------|--------------------|
| f_subpnt  | 1.72 s    | 0.46 s   | 3.77x        | 4.4x               |
| f_xdda    | 3.24 s    | 1.39 s   | 2.33x        | 3.5x               |
| f_dyn01   | 9.57 s    | 5.56 s   | 1.72x        | 2.1x               |

Every compute-bound family narrowed the gap to native (f_xdda by ~33%).
Family-level gains are smaller than the 1.95x microbenchmark because
families mix fixed-size dummies (which fold to a pointer) with
assumed-shape/assumed-size dummies (runtime extents kept) and spend time in
I/O / string / kernel-parse work outside the indexing hot path.

### Regressions the refactor left (found rebuilding at -O2, now fixed)

The shrink was pushed with the corpus validated on *stale* pre-refactor
``.cpp``; a fresh reconvert exposed helpers whose ArrayRef parameter still
named the old 3-parameter form (hard-coded ``Lower`` / missing
``Contiguous``/``Extents``), so the new static-contiguous dummy no longer
bound:
- **CharArrayRef**'s ``ArrayRef<FortranString<N>,...>`` ctor -- broke
  zzbodker/zzsrfker/zzbodini (in the corpus).  Fixed (deduce all params).
- **seq_assoc**'s contiguous overload pinned ``Lower=runtime_lower`` -- broke
  a contiguous static-lower actual.  Fixed (deduce ``Lower``).
With both fixes a fresh reconvert is corpus-clean again (1625/0).  Separately,
GCC 13.3 ICEs (``in modify_call, at ipa-param-manipulation``) at ``-O2`` on
the big GF routines with the new ``[[no_unique_address]]`` members;
``-fno-ipa-sra`` (still ``-O2``) is a clean workaround for those TUs.

---

## Perf items 2 & 3, the DO-bound bug, and the SPK type-01/21 precision FAIL

Following the f_xdda profiling (tiny-vector-op tax), two codegen levers landed:

- **#2 inline pure-leaf routines.**  A routine with no threaded state (no
  COMMON/SAVE/module/units/workspace parameter) and a small body -- the
  vector/matrix primitives -- is emitted ``inline`` in ``fortran_modules.hpp``
  instead of its ``.cpp``, so every caller inlines it across TU boundaries
  (recovers most of the whole-program-LTO win without LTO).  Gate:
  ``emit._is_inline_leaf`` (no ``state_params``/``workspace``/``save_struct``,
  not a template, not an ENTRY member, <= 60 statements).
- **#3 keep small fixed temps on the stack.**  ``_build_workspaces`` hoisted
  *every* static local array into the per-routine Workspace struct; a tiny
  literal-dimensioned temp (<= 64 elements: a ``R(3)`` vector, a ``M(3,3)``)
  now stays a stack ``ftn::Array`` -- no threaded parameter on the routine or
  its callers.  Large / named-dimension buffers still hoist.

Both are validated corpus-clean (1625/0) and drop the tspice binary ~35%
(430 MB -> 278 MB from inline dedup + lighter signatures).

**Counted-DO trip count must be frozen.**  A Fortran ``DO I = LO, HI`` fixes
its iteration count on entry; the naive ``for (i=lo; i<=hi; ++i)`` re-reads a
``HI`` the body reassigns.  The SPICE f_spk21 read-back ``DO I = J, K`` sets
``K = (I-1)*DLSIZE+1`` each pass, so the C++ loop ran past K and overran
TBUFF (``index 10100 out of range [1,10099]`` -> CRASH).  Lowering now detects
a bound variable assigned in the body (``IRDo.capture_bounds``) and emit
freezes the bound (and a variable step) into block-scoped ``const
ftn::index_t`` temps; ordinary loops keep the clean form.  This fixed
f_spk21's crash.

**f_spk01 / f_spk21 "recover states" FAIL: an ill-conditioned-solver
precision limit, not a fixable logic bug.**  Both fail Test Case "Recover
states from the type NN segment" with a relative error ~1.2e-9 against a
5e-12 tolerance.  Ruled out by elimination + instrumentation:
- **Not items 2/3** -- the isolation build (both disabled) fails identically.
- **spke01 (the evaluator) is exact** -- instrumenting it showed ``dt``,
  ``g``, ``refpos``, ``refvel`` matching the raw record to 17 digits, and
  spke01's output equals SPKEZ's (no frame rotation in the path).
- **DAF record I/O is exact** -- the ``'='`` round-trip test cases pass.
- The fit coefficients come from the test-utility chain ``T_T13XMD`` ->
  ``T_TAYHRM`` -> ``T_SOLVEG_2``: Gaussian elimination on a 2N=16
  Vandermonde-like Hermite matrix (highly ill-conditioned).  Position
  component **x is exact, y/z are off by ~1e-9** -- the signature of the
  ill-conditioned solve amplifying a ULP-level expression-ordering difference
  between the C++ and the reference Fortran.  No ``float`` contamination in
  the chain.

At ``-O0`` the double arithmetic is deterministic, so the ArrayRef refactor
(layout-only, same integer offsets) cannot have changed the computed values;
these families almost certainly just started *linking* (367 vs 365 live) and
exposed a pre-existing precision limitation.  A fix would require bit-exact
reproduction of Fortran's operation order inside a Gaussian solver, which is
impractical to guarantee -- left documented rather than chased.  Net tspice
with the crash fixed: PASS 342, TIMEOUT 23 (known slow families), FAIL 2,
CRASH 0.
