# IRI Fortran-vs-C++ comparison

Confirms that the converter produces byte-identical Fortran-runtime
output for all five **IRI** ionosphere model releases (2001, 2007, 2012,
2016, 2020) with the SPICE C++20 runtime.  Run with:

```bash
bash docs/iri_diff.sh
```

It builds each version's `iritest` driver twice — once with `gfortran`
(reference) and once via the converter + `g++` — feeds the same scripted
input to both, and compares `fort.7`.  Per-version inputs differ because
the `iritest` drivers ask their questions in different orders.

## Prerequisites

- `gfortran`
- `g++` with `-std=c++20`
- This project's `flang` build at `build/bin/flang` (provides the AST
  dumper)
- The IRI distributions at `/tmp/fx/iri_{2001,2007,2012,2016,2020}/`
- `/tmp/apf107.dat` -- the operational geomagnetic / F10.7 index file,
  not in the IRI distributions; fetch from
  `https://irimodel.org/indices/apf107.dat` (the script auto-copies it
  into each version's directory when present).

The script automatically borrows `ccir*.asc` / `ursi*.asc` map files from
`iri_2020` for versions that don't ship them (notably `iri_2016`), and
`ap.dat` + `ig_rz.dat` from `iri_2007` for `iri_2001`.  These are
ancillary data files, not generated artifacts.

## Per-version notes

- **iri_2001** uses year 1990 (not 2000): the bundled `igrf10.dat` is a
  newer-format file with `NMAX=13`, but the iri_2001 source's coefficient
  arrays only hold up to `NMAX=11`.  Year 1990 reads `dgrf90`/`dgrf95`
  which are `NMAX=10`.  This is a *known data-vs-source mismatch in the
  IRI-2001 distribution*, not a converter issue.
- **iri_2012** needs the `corpus_fixes/iri_2012/` patches for DEC
  `TYPE *` statements (extension flang rejects).  The script substitutes
  patched sources automatically.
- **iri_2016** doesn't ship CCIR/URSI map files; the script copies them
  from `iri_2020`.

## Results

Each line of output looks like:

```
=== iri2020 ===
  Fortran build...
  C++ convert OK
  ✓ EXACT MATCH (37 lines)
```

The match is `cmp -s` byte-exact on `fort.7`.  A passing run produces
identical MD5 sums for the Fortran and C++ `fort.7` (see commit
`d4731d92f` for the iri_2020 reference MD5).
