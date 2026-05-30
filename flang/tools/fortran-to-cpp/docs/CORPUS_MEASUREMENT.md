# Measuring the corpus

The end-to-end check is "convert every Fortran file in our corpora,
chunk-compile the generated C++ with `-fsyntax-only`, and count errors."
Tooling lives in `/tmp` because it's session-local and changes
occasionally — paste these scripts when you need them.

> The expected current state, with `corpus_fixes/` substituted in:
> **1625 files, 0 errors.**

---

## Layout

```
/tmp/fx/                            ← Fortran source corpora (NASA SPICE +
                                      IRI / MSIS / IGRF / radbelt / CIRA).
                                      Not part of this repo; expected pre-cloned.
/tmp/tool_out/<name>/               ← generated .cpp + shared header
/tmp/tool_out/<name>_chunks/        ← per-chunk error logs
/tmp/tool_out/<name>_err.txt        ← consolidated error log
```

---

## The two scripts

Drop these in `/tmp` at the start of a session:

### `/tmp/tool_measure_fixed.py` — convert

```python
import sys, os, glob, time
sys.path[:0] = [".", "/home/user/llvm-project/flang/tools/flang-ast-py"]
os.environ.setdefault("FLANG", "/home/user/llvm-project/build/bin/flang")
from converter.project import convert_files
FL = os.environ["FLANG"]
SRC = "/tmp/fx"
FIX = "/home/user/llvm-project/flang/tools/fortran-to-cpp/corpus_fixes"
comps = sys.argv[1].split(",")
outd = "/tmp/tool_out/" + (sys.argv[2] if len(sys.argv) > 2 else "out")
files = []
for c in comps:
    for f in sorted(glob.glob(f"{SRC}/spice_toolkit/src/{c}/*.for")):
        rel = os.path.relpath(f, SRC)
        fixed = os.path.join(FIX, rel)
        files.append(fixed if os.path.exists(fixed) else f)
print(f"using fixed: {sum(1 for f in files if f.startswith(FIX))}/{len(files)}")
os.makedirs(outd, exist_ok=True); t0 = time.time()
res = convert_files(files, flang=FL)
n = 0
for p, txt in res.items():
    name = os.path.basename(str(p))
    if not name.endswith((".cpp", ".hpp")):
        name = os.path.splitext(name)[0] + ".cpp"
    open(os.path.join(outd, name), "w").write(txt)
    n += 1
print(f"converted {len(files)} -> {n} in {time.time()-t0:.1f}s into {outd}")
```

The script always substitutes `corpus_fixes/<path>` for the matching
original whenever a patched version exists, so the result reflects the
state we publish.

### `/tmp/tool_chunk.py` — chunk-compile

```python
import sys, os, glob, subprocess
OUT = sys.argv[1]
RT = "/home/user/llvm-project/flang/tools/fortran-to-cpp/runtime/include"
CD = OUT + "_chunks"; os.makedirs(CD, exist_ok=True)
only = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None
SRC = "/tmp/fx/spice_toolkit/src"
def comp_of(stem):
    for c in os.listdir(SRC):
        if os.path.exists(f"{SRC}/{c}/{stem}.for"):
            return c
    return None
allf = sorted(glob.glob(OUT + "/*.cpp"))
files = [
    f for f in allf
    if (only is None or comp_of(os.path.splitext(os.path.basename(f))[0]) in only)
]
CH = int(os.environ.get("CHUNK", "100"))
nch = (len(files) + CH - 1) // CH
for ci in range(nch):
    cf = f"{CD}/cnt_{ci}.txt"
    if os.path.exists(cf):
        continue
    tot = nf = 0; lines = []
    for f in files[ci * CH : (ci + 1) * CH]:
        r = subprocess.run(
            ["g++", "-std=c++20", "-fsyntax-only", "-I", RT, "-I", OUT, f],
            capture_output=True, text=True,
        )
        es = [l for l in r.stderr.splitlines() if ": error:" in l]
        if es:
            tot += len(es); nf += 1; lines += es
    open(f"{CD}/err_{ci}.txt", "w").write("\n".join(lines) + ("\n" if lines else ""))
    open(cf, "w").write(f"{nf} {tot}\n")
    print(f"chunk {ci}/{nch-1}: {nf} files, {tot} errors", flush=True)
tnf = ttot = 0
for c in glob.glob(f"{CD}/cnt_*.txt"):
    a, b = open(c).read().split()
    tnf += int(a); ttot += int(b)
os.system(f"cat {CD}/err_*.txt > {OUT}_err.txt")
print(f"FULL {OUT}: {len(files)} files, {tnf} with errors, {ttot} total errors")
```

Notable design choices:

- **Resumable.**  Each chunk writes a `cnt_<i>.txt` once it's done; a rerun
  skips chunks already counted.  Useful when the container kills the build
  partway through (it happens).
- **Per-file `-fsyntax-only`.**  Doesn't link.  This is enough to confirm
  the generated C++ type-checks; linking is for downstream.
- **CHUNK size 100** balances parallelism vs memory.  Lower it via
  `CHUNK=50 python3 /tmp/tool_chunk.py …` if the container is tight on
  RAM.

---

## End-to-end recipe

From a clean state:

```bash
# 1. (Re-)create the scripts above if /tmp was wiped.
cd flang/tools/fortran-to-cpp
rm -rf /tmp/tool_out                                   # idempotent reset
python3 /tmp/tool_measure_fixed.py component,support,spicelib support_fixed
#   ~3-4 min on this container; expected: "1625 -> 1626"
CHUNK=100 python3 /tmp/tool_chunk.py /tmp/tool_out/support_fixed component,support,spicelib
#   ~3-5 min; expected: "FULL ...: 1625 files, 0 with errors, 0 total errors"
```

The `1626` output count is `1625 .cpp` + `1 fortran_modules.hpp` (shared
header).

To compile **without** the corpus patches (to confirm a regression is in
the converter, not in your code), use `tool_measure.py` (without `_fixed`)
which just walks `/tmp/fx` directly.  Expected:
**1623 files, 6 with errors, 8 total errors** (all `txtopr` / `zzascii`
residuals; documented in [`NONSTANDARD_SOURCE.md`](NONSTANDARD_SOURCE.md)).

---

## Reading the error log

After a run, `/tmp/tool_out/<name>_err.txt` has every `: error:` line.
Quick triage:

```bash
# Errors by file
sed 's|/tmp/tool_out/[^/]*/||g' /tmp/tool_out/<name>_err.txt \
  | grep ": error:" | grep -oE '^[a-z0-9_]+\.cpp' | sort | uniq -c | sort -rn

# Distinct error texts (for cascading template instantiations)
sed -E 's|/tmp/tool_out/[^/]*/||g; s/from expression of type.*/[elided]/' \
  /tmp/tool_out/<name>_err.txt | grep ": error:" | sort -u | head
```

A cascade in `fortran_modules.hpp` (the shared header) almost always
means a single broken template instantiation echoing across many `.cpp`
files.  Fix the one root cause, not the apparent multitude.

---

## Non-SPICE corpora

The IRI/MSIS/IGRF/radbelt/CIRA models live alongside SPICE under
`/tmp/fx/<corpus>/`.  The `convert_files` path handles each independently
(no shared symbols across corpora).  A handful of individual files need
the `corpus_fixes/` patches to parse — see `corpus_fixes/README.md`.

Useful one-liner to count Fortran files per non-SPICE corpus:

```bash
cd /tmp/fx
for d in */; do
  d=${d%/}
  [ "$d" = "spice_toolkit" ] && continue
  [ "$d" = ".git" ] && continue
  n=$(find "$d" -iname '*.for' -o -iname '*.f' -o -iname '*.f90' -o -iname '*.F90' 2>/dev/null | wc -l)
  [ "$n" -gt 0 ] && echo "$d: $n"
done
```
