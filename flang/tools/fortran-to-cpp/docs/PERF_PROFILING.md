# Performance profiling of the converted SPICE code

This note records where the *optimized* (`-O2 -DNDEBUG`) translated code
spends its time, and the approaches that follow, so performance work is
data-driven rather than guessed.  It complements the ArrayRef-shrink work
recorded in `JOURNAL.md` (which removed the **indexing** hot path); the
findings here are about what remains *after* that.

## Method

- Reconvert the tspice corpus, build a separate optimized binary
  (`tspice_opt`) with `g++ -std=c++20 -O2 -DNDEBUG -g -fno-ipa-sra`
  (the `-fno-ipa-sra` avoids a GCC 13.3 ICE on the big GF routines with
  the `[[no_unique_address]]` ArrayRef members).
- Profile a representative compute-bound family under callgrind:
  `valgrind --tool=callgrind tspice_opt f_subpnt`, then
  `callgrind_annotate --tree=caller`.

At `-O2` the "TIMEOUT" families are *not* broken — they finish quickly
(`f_subpnt` 1.3 s, `f_dyn01` 6.4 s, `f_gfrr` 11.4 s); the tspice timeouts
are an artifact of the `-O0` measurement build (no inlining, live bounds
checks).  So this profiling targets the *residual* gap to native gfortran
(measured elsewhere at ~1.7–3.8x).

## Findings (f_subpnt, 5.19 G instructions)

| share | site | nature |
|------:|------|--------|
| **27.4%** | `__memset_avx2_unaligned_erms` | dead/needless zero-or-blank fills (see below) |
| ~14.6% | inside `chkin`/`chkout` (via the above memset) | blank-init of error-path-only `CHARACTER` locals |
| ~5.4% | `zzinilnk` (via memset) | workspace (link-table) zeroing during DSK voxel setup |
| ~10% | `chkin` + `chkout` bodies | the `CHKIN`/`CHKOUT` traceback stack machinery |
| ~2.7% | `__sincos_fma` | genuine trig |
| remainder | `dasrri`/`dasuri`/`dafgdr`, `nearpt`, `prop2b`, `stmp03`, `zzmkspin`, `zzvoxcvo` | genuine DAS record I/O + geometry |

The ArrayRef index math does **not** appear as a hotspot — the static
lower/stride/extents NTTPs (see JOURNAL) already fold it away; the dummy
views show up in the symbols as bare pointers.

### The #1 residual cost: dead blank-init of CHARACTER locals

`memset` is **27%** of the run, and it is almost entirely the blank fill
that `ftn::FortranString<N>`'s default constructor does (`data_.fill(' ')`,
matching gfortran `-finit-character=32`).  The worst offenders are large
scratch strings that are only touched on an *error* path but are
default-constructed (and so blank-filled) on **every** call:

- `CHKIN`/`CHKOUT` (`trcpkg.for`) declare `DEVICE*(255)`, `TMPNAM*(80)`,
  `STRING*(11)` — ~346 bytes blank-filled per call.  On the common path
  they are never read; on the error path they are written (`GETDEV(DEVICE)`,
  `TMPNAM = MODULE(...)`) before any read.  Either way the fill is dead.
  Since CHKIN/CHKOUT run once per SPICE routine entry/exit, this is ~15% of
  a compute family's instructions.

An A/B on the runtime confirmed the mechanism: replacing those three locals'
`{}` (blank-fill) construction with a no-fill construction removes their
memset entirely.

## Approaches (prioritized)

1. **Elide the dead blank-init.**  Emit a no-fill construction
   (`ftn::FortranString<N>(ftn::uninit)`) for a `CHARACTER` local the
   converter can prove is *definitely assigned (whole-variable) before any
   read*.  Expected ~15–27% on compute-bound families.
   - Enabler (runtime): a `FortranString(uninit_t)` constructor that skips
     `data_.fill(' ')`.  Trivial and ABI-neutral.
   - Gate (converter): a **definite-assignment** dataflow pass over the
     structured IR — a var is skippable iff every read is dominated by a
     whole-variable write (whole-var `=` target, or an `intent(out)`
     argument position).  Must treat substring writes / reads-as-args
     conservatively and **bail to blank-init on any residual goto-dispatch**
     — a false "definitely assigned" would silently read uninitialized
     memory in a numerics library, so correctness must dominate the win.
     This is the reason it is *not* landed here: it needs careful,
     supervised implementation and validation, not an unsupervised guess.

2. **Make CHKIN/CHKOUT (traceback) compile-out-able (~10%).**  The
   traceback stack is a debugging aid.  A build-time switch that lowers
   `CHKIN`/`CHKOUT` (and the `STACK(...) = MODULE` push / padded-compare
   pop) to no-ops — analogous to the `NDEBUG` bounds-check gate — removes
   per-routine entry/exit overhead.  It is SPICE library code (converted
   Fortran, not runtime), so the cleanest lever is a converter option that
   drops calls to a known set of trace routines, or a runtime shim.

3. **Confirm large workspace zeroing lowers to `memset`.**  `zzinilnk`-style
   `DO I=1,N: A(I)=0` loops over a contiguous array should vectorize to a
   single `memset`; a strided target (`A(1,I)=0` over a 2-row array) cannot
   and stays a scalar loop — check the emit keeps these contiguous where
   the Fortran is.

4. **Measurement (not a code change):** the tspice tally's TIMEOUTs are the
   `-O0` build.  Running the RUN phase with `-O2 -DNDEBUG` (crash
   classification can stay on the `-O0` build) would show 0 timeouts.

## Second family: f_xdda (DSK ray traversal) — the tiny-vector-op tax

Profiling a *different* compute-bound family (`f_xdda`, XDDA voxel ray
traversal; 23.3 G instructions) shows a hotspot profile unlike f_subpnt's
memset dominance.  After the ray/voxel routines (`zzraybox` 21.8%, `surfpt`
9.3%, `xdda` 4.5%) and test-harness code (`t_chkvox`, `chcksc`, `zzrepsub`
string substitution ~7%), the striking cost is the **small vector library**:

| routine | share |
|---------|------:|
| `vnorm` | 5.4% |
| `vperp` | 4.3% |
| `vproj` | 4.2% |
| `vhat`  | 2.4% |
| `vsub`  | 1.7% |
| `vlcom` | 1.7% |
| ...     | (memset only 5.7% here) |

**~20% of the run is 3-element vector ops** — each a separate `.cpp`
function, called millions of times, that at `-O2` (no LTO) **cannot be
inlined into its hot caller**.  Two translation-specific overheads pile on
top of the raw call: the `ArrayRef` descriptor args, and — for `vperp` /
`vproj` / `mxv` etc. — a `Workspace&` param that hoists a 3-element local
temp (`DOUBLE PRECISION R(3)` in Fortran) into a caller-owned struct.

### Isolated A/B (cross-TU, `-O2 -DNDEBUG`, vproj-shaped kernel)

| variant                        | no LTO  | `-flto` |
|--------------------------------|--------:|--------:|
| workspace param (as emitted)   | 1808 ms |  267 ms |
| stack-local temp               | 1665 ms |  184 ms |
| header-inline                  | 1039 ms |  188 ms |

- **LTO is the big lever: 6.8x** on the workspace variant — it inlines the
  leaf op across the TU boundary and even sees through the workspace param.
- **Stack-local temps: ~8%** on their own (and remove the threaded param,
  which also lightens every caller).
- **Header-inline: 1.74x** without LTO — most of the LTO win, targeted.

### Approaches (new, prioritized) — closing the compute-bound gap

5. **Build with `-flto`.**  The single biggest lever for the geometry
   families: the SPICE vector/matrix leaf library (`vadd`/`vsub`/`vhat`/
   `vnorm`/`vdot`/`vproj`/`vperp`/`vlcom`/`mxv`/...) inlines into the hot
   callers, folding away the call + descriptor + workspace overhead.  A fair
   gfortran baseline would also use `-flto`, but the converted code has more
   per-call overhead to reclaim, so it benefits more.  (Cost: whole-program
   LTO link of ~2000 TUs is memory/time heavy — verify feasibility.)

6. **Emit the tiny pure leaf routines as header `inline` functions** rather
   than separate `.cpp` files.  Gets most of the LTO benefit for the hottest
   ops *without* a whole-program LTO build — a routine with no COMMON/SAVE,
   no I/O, no error-signalling, and only small fixed-size local temps is a
   safe candidate (the vector/matrix primitives are exactly this shape).

7. **Don't hoist *small* fixed-size local arrays into a `Workspace`.**  A
   non-SAVE `R(3)` temp in a leaf routine should be a stack `std::array`, not
   a threaded workspace member (~8% on the op, plus it removes the `Workspace&`
   param from hot routines and improves both LTO and non-LTO codegen).  Gate
   on a small element-count threshold; keep large scratch arrays hoisted.
