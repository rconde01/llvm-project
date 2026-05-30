# Standard-conforming patches for the test corpus

This tree mirrors the source layout of the original corpora and contains
**minimal, standard-conforming edits** to every file that `flang` refused
to parse or semantically check.  Each edit removes a vendor-extension or
deleted-feature construct in favor of the standard equivalent that produces
the same behavior — see [`../docs/NONSTANDARD_SOURCE.md`](../docs/NONSTANDARD_SOURCE.md)
for the diagnostic, the extension, and which compilers accept the original.

> Why patch the *source* rather than the converter?  The converter only
> ever sees what `flang` can parse.  Each construct here is non-standard
> Fortran (vendor extension or a feature deleted from the language), so
> the language-level fix lets every standard-conforming Fortran compiler —
> `flang` included — read the code.

## Files patched

```
spice_toolkit/src/spicelib/txtopr.for
spice_toolkit/src/spicelib/zzascii.for
iri_2007/IMAZ/iri_imaz.for
iri_2012/iriorbit.for
iri_2012/iriorbitmax.for
iri_2012/irifun.for
radbelt/radbelt.for
```

Every file in this tree has been re-checked with
`flang -fc1 -fdebug-dump-analyzed-tree-json <file>` and reports **no errors**.

---

## The five edits

### 1. `MODE='READ'` → `ACTION='READ'` in `OPEN`

*Files:* `txtopr.for`, `zzascii.for`

```
flang: error: Could not parse ...
       txtopr.for:392:46: error: expected '='
```

`MODE=` is a non-standard `OPEN` connect specifier from the DEC / Compaq /
Intel Fortran lineage; the standard equivalent that means the same thing —
restrict the unit to reads — is `ACTION='READ'`.

Before:
```fortran
     .       STATUS          = 'OLD',
     .       MODE            = 'READ',
     .       IOSTAT          =  IOSTAT      )
```
After:
```fortran
     .       STATUS          = 'OLD',
     .       ACTION          = 'READ',
     .       IOSTAT          =  IOSTAT      )
```

Behavior preserved exactly: both forms open the file read-only.

---

### 2. `TYPE *, …` output → `PRINT *, …`

*Files:* `iriorbit.for`, `iriorbitmax.for`

```
flang: error: Could not parse ...
       iriorbit.for:38:11: error: expected '=>'
```

`TYPE` as an output statement is a DEC / VAX extension synonymous with
`PRINT`.  In standard Fortran `TYPE` introduces a derived-type definition,
so `flang` parses `type *, …` as a declaration and fails on the `*`.  Every
occurrence (the source uses both spaced `type *` and unspaced `type*`) is
replaced with `print *` / `print*`:

Before:
```fortran
        type *,'name of file with orbit information'
4321    type*,'ERROR'
```
After:
```fortran
        print *,'name of file with orbit information'
4321    print*,'ERROR'
```

Behavior preserved: `PRINT *, list` writes the same list to the standard
output unit.

---

### 3. `REAL` `DO`/subscript indices → integer

*Files:* `radbelt.for`, `iri_imaz.for`

```
flang: radbelt.for:325:9 : error: Must have INTEGER type, but is REAL(4)
       iri_imaz.for:1895:35: error: Must have INTEGER type, but is REAL(4)
```

`REAL` `DO` control variables were marked obsolescent in Fortran 90 and
**deleted in Fortran 95**; `REAL` array subscripts were never standard.
gfortran (`-std=legacy`) and Intel Fortran still accept both, silently
truncating to `INT()`.  The fix is to make the index an integer.

#### `radbelt.for`

In this file the offending name is also used as a **real elsewhere**
(`EI = E(K+1)`, `IF (EI .GT. 0.10)`), so retyping `EI` would break its
other roles.  Introduce a separate integer counter for the loop.  A name
in `I`–`N` is integer under default implicit typing, so renaming to `II`
needs no declaration:

Before:
```fortran
1898  DO 1779 EI = NE+1, 6
1779     E(EI) = 0.0
```
After:
```fortran
1898  DO 1779 II = NE+1, 6
1779     E(II) = 0.0
```

#### `iri_imaz.for`

This file has two patterns.  The first reuses the real scalar `m` (declared
`real mndd(2), m, c, …`) as an integer implied-DO index — almost certainly
a name collision.  Rename it to a fresh integer-by-implicit name:

Before:
```fortran
        read(12,*) (pres60(N,M), M=1,13)
        read(12,*) (pres70(N,M), M=1,49)
```
After:
```fortran
        read(12,*) (pres60(N,jj), jj=1,13)
        read(12,*) (pres70(N,jj), jj=1,49)
```

The second uses the real scalars `a` and `W` (both in the implicit-real
range) as subscripts of `pres60` / `pres70`.  Both names are reused
elsewhere as reals, so wrap the subscript in `INT()` — matching exactly
what the legacy compilers do implicitly:

Before:
```fortran
           epr = pres60(a,  mm)
           epr = pres70(a,  W)
```
After:
```fortran
           epr = pres60(int(a), mm)
           epr = pres70(int(a), int(W))
```

Behavior preserved: in every case the integer index is the truncation of
the original real index, which is exactly what the accepting compilers
produced.

---

### 4. Duplicate `DATA` initialization of blank `COMMON` → keep one

*File:* `irifun.for`

```
flang: irifun.for:5582:25: error: Multiple initialization of COMMON block //
```

The blank common block is `DATA`-initialized in **two different
subprograms** (lines 5414 and 5588 in the original), with **byte-identical**
`DATA` statements — both setting the same six members to the same defaults.
The Fortran standard allows initialization of a common block only once and
only in a `BLOCK DATA` program unit (blank common at all is non-standard,
but `flang` accepts a single one as an extension).  Multiple initializations
are flagged.

Because the two `DATA` statements are byte-identical, the standard-equivalent
edit is to delete the duplicate.  `DATA` is one-shot at program load, so a
second identical `DATA` is a no-op even on the compilers that accept it.

Before (two distinct subprograms each contained):
```fortran
      COMMON  BINT, BEXT, RE, TZERO, IFIT, IB, KINT, LINT, KEXT,
     *              LEXT, KMAX, FN
      ...
      DATA THETA, RE,    TZERO, IFIT, ICEN, IREF, IB, KINT, LINT, KEXT, LEXT
     *     /180., 6371.2, 1.0,  -1,   0,    0,    2,  6,    4,    0,    -1/
```
After: the second subprogram still declares the `COMMON`, but its
duplicate `DATA` is removed; the first subprogram's `DATA` runs once at
load and supplies the values to every routine sharing the storage.

Behavior preserved: identical to running on a compiler that accepted the
duplicate and discarded the second initialization.

> A stricter, fully standard-conforming alternative is to give the block
> a name (`/IFIT_DEFAULTS/`) in every declaration and move the single
> `DATA` into a dedicated `BLOCK DATA` program unit.  The minimal edit
> above is sufficient for `flang`.

---

## Result (measured)

| Corpus state | Files compiled | Files with errors | Total errors |
|---|---:|---:|---:|
| Original SPICE corpus (`component + support + spicelib`) | 1623 | 6 | 8 |
| With the `corpus_fixes/` patches applied | **1625** | **0** | **0** |

The 8 errors in the un-patched run were all *consequences* of the two
unparseable source files: other files calling routines from `txtopr.for` /
`zzascii.for`, which never got converted, so the call sites named
undeclared symbols.  With those two source files parsing, their `.cpp`
outputs exist (count rises from 1623 to 1625) and every call resolves
cleanly across the whole corpus.

The IRI / radbelt patches in this tree are not part of the SPICE
chunk-compile above; each has been individually verified to make `flang`
parse the file (`PARSED-OK`), which is the prerequisite for the converter
to ingest it.
