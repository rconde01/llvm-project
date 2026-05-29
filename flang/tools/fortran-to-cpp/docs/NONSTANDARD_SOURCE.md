# Non-standard source constructs flang rejects

A handful of corpus files don't convert because **flang refuses to parse or
semantically check them** — not because the converter has a gap.  Each uses
a construct that some older or vendor Fortran compilers accept (often only
with a legacy/extension flag) but that violates the Fortran standard, so
LLVM `flang` reports a hard error and produces no parse tree for the
converter to lower.

This document records, for every such construct found in the corpus:

* the exact `flang` diagnostic,
* why other compilers accept it (the extension and its behavior),
* the minimal source edit that makes the code standard-conforming so flang
  (and therefore the converter) accepts it without changing the program's
  meaning.

These edits belong in the **original Fortran**, not in the converter: the
converter only ever sees what flang can parse.

> How to reproduce a diagnostic:
> ```
> flang -fc1 -fdebug-dump-parse-tree-json <file>.for
> ```

---

## 1. `MODE=` connect specifier in `OPEN`

**Files:** `spicelib/txtopr.for`, `spicelib/zzascii.for`

**Diagnostic:**
```
error: Could not parse .../txtopr.for
txtopr.for:392:46: error: expected '='
```

**Source:**
```fortran
      OPEN ( UNIT            =  UNIT,
     .       FILE            =  FILE,
     .       FORM            = 'FORMATTED',
     .       ACCESS          = 'SEQUENTIAL',
     .       STATUS          = 'OLD',
     .       MODE            = 'READ',          ! <-- here
     .       IOSTAT          =  IOSTAT      )
```

**Why other compilers accept it.** `MODE=` is a non-standard connect
specifier from the DEC/Compaq/Intel Fortran lineage that declares the
intended file-access mode (`'READ'`, `'WRITE'`, `'READWRITE'`).  Those
compilers parse it as a vendor extension; on most it is functionally
identical to the standard `ACTION=` specifier.  `flang` implements only the
standard connect specifiers, so when it reaches the `MODE` keyword it is not
a recognized specifier and the parser fails ("expected `=`" because nothing
valid can follow).

**Standard-conforming fix.** Replace `MODE=` with the standard `ACTION=`
specifier, which has the same meaning and the same allowed values:
```fortran
     .       ACTION          = 'READ',
```
(If the surrounding code never relied on the access restriction, the line
can simply be deleted; `ACTION` defaults to `'READWRITE'`.)  Verified:
swapping `MODE='READ'` for `ACTION='READ'` makes flang parse `zzascii.for`
(`PARSED-OK`).

---

## 2. `TYPE` as an output statement

**Files:** `iri_2012/iriorbit.for`, `iri_2012/iriorbitmax.for`

**Diagnostic:**
```
error: Could not parse .../iriorbit.for
iriorbit.for:38:11: error: expected '=>'
iriorbit.for:38:11: error: expected '('
iriorbit.for:38:11: error: expected '='
iriorbit.for:38:11: error: expected ':'
```

**Source:**
```fortran
        type *,'name of file with orbit information'
        read(5,*) orbit_input
```

**Why other compilers accept it.** `TYPE` (as in `TYPE *, list` or
`TYPE fmt, list`) is a DEC/VAX Fortran statement that writes to the standard
output unit — a direct synonym for `PRINT`.  gfortran (`-fdec`) and Intel
Fortran keep it for backward compatibility.  In standard Fortran `TYPE` is
*only* the keyword that introduces a derived-type definition or a
`TYPE(name)` declaration, so flang tries to parse `type *, ...` as a
declaration: it sees the identifier `type`, then expects `=>`, `(`, `=`, or
`:` to continue a declaration/definition, and fails on the `*`.

**Standard-conforming fix.** Replace `TYPE` with `PRINT` (identical
semantics and argument syntax).  It appears both spaced and unspaced
(`type *,` and `type*,`), so replace every occurrence:
```fortran
        print *,'name of file with orbit information'
```
Verified: replacing all `TYPE*`/`TYPE *` in `iriorbit.for` makes flang parse
the file (`PARSED-OK`).

---

## 3. `REAL` variable used as a `DO` index or array subscript

**Files:** `radbelt/radbelt.for`, `iri_2007/IMAZ/iri_imaz.for`

**Diagnostic:**
```
radbelt.for:325:9: error: Must have INTEGER type, but is REAL(4)
iri_imaz.for:1895:35: error: Must have INTEGER type, but is REAL(4)
```

**Source — `radbelt.for` (implicitly typed index):**
```fortran
1898  DO 1779 EI=NE+1,6        ! EI starts with 'E' -> implicit REAL
1779     E(EI)=0.0             ! REAL value used as an array subscript
```

**Source — `iri_imaz.for` (explicitly REAL name reused as an index):**
```fortran
        real mndd(2), m, c, WN, t, d         ! m is REAL here
        ...
        read(12,*) (pres60(N,M),M=1,13)      ! M used as subscript & implied-DO var
```

**Why other compilers accept it.** Real `DO` control variables were part of
FORTRAN 77, marked obsolescent in Fortran 90 and **deleted in Fortran 95**;
real array subscripts were never standard but were a widespread extension.
gfortran (with `-std=legacy`) and Intel Fortran accept both, silently
converting the real value to integer by truncation toward zero
(`INT(x)`).  flang implements the current standard, where a `DO` variable
and every array subscript must be of integer type, so it errors.

**Standard-conforming fix.** Use an *integer* index for the loop/subscript.
Note that in both files the offending name is also used as a real elsewhere
(`radbelt`'s `EI` appears as `EI=E(K+1)` and `IF(EI.GT.0.10)`; `iri_imaz`'s
`m` is a real scalar), so the index must be a **separate** integer variable
— retyping the existing name would break its real uses:

* `radbelt.for` — introduce a dedicated integer counter for this loop.  A
  name in `I`–`N` is integer under the default implicit typing, so renaming
  the index to `II` needs no declaration:
  ```fortran
        DO 1779 II = NE+1, 6
1779       E(II) = 0.0
  ```
  Verified: this clears the `radbelt.for:325` error.
* `iri_imaz.for` — the real scalar `m` is being reused as an integer loop
  index, which is almost certainly a naming accident.  Use a distinct
  integer index (and keep `m` for its real role):
  ```fortran
        read(12,*) (pres60(N,jj), jj=1,13)   ! jj is implicitly INTEGER
  ```
  Verified: this clears the `iri_imaz.for:1895` error.  If the intent really
  was to index by a computed real value, wrap it
  explicitly: `pres60(N, INT(m))` — matching what the legacy compilers did
  implicitly.

---

## 4. Initializing `COMMON` outside a single `BLOCK DATA`

**File:** `iri_2012/irifun.for`

**Diagnostic:**
```
irifun.for:5582:25: error: Multiple initialization of COMMON block //
```

**Source.** The blank common block is `DATA`-initialized in more than one
subprogram (lines 5414 and 5588 both run a `DATA` over members of the same
blank common declared at 5582):
```fortran
      COMMON  BINT,BEXT,RE,TZERO,IFIT,IB,KINT,LINT,KEXT,
     *              LEXT,KMAX,FN
      ...
      DATA THETA,RE,    TZERO,IFIT,ICEN,IREF,IB,KINT,LINT,KEXT,LEXT
     *     /.../
```

**Why other compilers accept it.** The standard allows a common block to be
initialized **only in a `BLOCK DATA` program unit, and only once across the
whole program**; blank common may not be initialized at all.  Many compilers
(gfortran, Intel) relax this and let a regular subprogram `DATA`-initialize
common storage, taking the first initialization they see.  flang enforces
the rule and reports the second initialization of the same (blank) block as
a conflict.

**Standard-conforming fix.** Move the initialization into a single
`BLOCK DATA` unit and give the block a name (blank common cannot be
initialized at all):
```fortran
      BLOCK DATA legmod_init
      COMMON /LEGMOD/ BINT, BEXT, RE, TZERO, IFIT, IB,
     *                KINT, LINT, KEXT, LEXT, KMAX, FN
      DATA RE, TZERO, IFIT /.../    ! the initialized members, once
      END
```
and change every `COMMON  BINT,BEXT,...` (blank) to the named
`COMMON /LEGMOD/ BINT,BEXT,...` so all units share the named block.  Remove
the duplicate `DATA` statement from the second subprogram.

---

## Summary

| Construct | Files | Standard fix |
|---|---|---|
| `MODE=` in `OPEN` | txtopr, zzascii | use `ACTION=` (or delete) |
| `TYPE *, …` output | iriorbit, iriorbitmax | use `PRINT *, …` |
| `REAL` `DO`/subscript | radbelt, iri_imaz | make the index `INTEGER` (or `INT(...)`) |
| `COMMON` init outside one `BLOCK DATA` | irifun | one named `BLOCK DATA`, init once |

All of these are deleted features or vendor extensions: the listed compilers
accept them (often only under a legacy flag) and apply the behavior
described above.  Applying the standard-conforming edit preserves that
behavior while letting flang parse the source, after which the converter
handles the file like any other.
