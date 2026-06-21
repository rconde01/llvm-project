#!/bin/bash
# Run Fortran + C++ for each IRI version, diff the outputs.
set -u
RT=/home/user/llvm-project/flang/tools/fortran-to-cpp/runtime/include
FIX=/home/user/llvm-project/flang/tools/fortran-to-cpp/corpus_fixes

# Versions vary in input order; pass per-version.
INPUT_2020='0
45.0 90.0
2000 0606 0 12.0
300.0
1
100. 300. 100.
0
0 0
0
'
# iri_2001/2007: hx, htec_max, ivar, vbeg/vend/vstp, jchoice
# Year 1990 for iri_2001 to avoid the NMAX=13 igrf00.dat (its arrays only
# go up to NMAX=11; dgrf90+95 cover this year with NMAX=10).
INPUT_2001='0
45.0 90.0
1990 0606 0 12.0
300.0
0
1
100. 300. 100.
0
'
# iri_2007: year 2000 with default jchoice path.
INPUT_2007='0
45.0 90.0
2000 0606 0 12.0
300.0
0
1
100. 300. 100.
0
'
# iri_2012: hx, piktab, htec_max, ivar, vbeg/vend/vstp, jchoice
INPUT_2012='0
45.0 90.0
2000 0606 0 12.0
300.0
0
0
1
100. 300. 100.
0
'

run_version() {
  local NAME="$1"; local SRCDIR="$2"; local IN="$3"; local DRIVER="${4:-iritest}"
  local FDIR="/tmp/v/${NAME}_f" CDIR="/tmp/v/${NAME}_c"
  mkdir -p "$FDIR" "$CDIR"

  # data files
  cp "$SRCDIR"/*.dat "$SRCDIR"/*.asc "$FDIR/" 2>/dev/null
  cp "$SRCDIR"/*.dat "$SRCDIR"/*.asc "$CDIR/" 2>/dev/null
  [ -f /tmp/apf107.dat ] && { cp /tmp/apf107.dat "$FDIR/"; cp /tmp/apf107.dat "$CDIR/"; }
  [ -f /tmp/fx/iri_2007/ig_rz.dat ] && { cp /tmp/fx/iri_2007/ig_rz.dat "$FDIR/" 2>/dev/null; cp /tmp/fx/iri_2007/ig_rz.dat "$CDIR/" 2>/dev/null; }
  # iri_2001 wants ap.dat (older format the newer apf107.dat doesn't replace).
  [ -f /tmp/fx/iri_2007/ap.dat ] && { cp /tmp/fx/iri_2007/ap.dat "$FDIR/" 2>/dev/null; cp /tmp/fx/iri_2007/ap.dat "$CDIR/" 2>/dev/null; }
  # Some versions (iri_2016) don't ship ccir/ursi map files; borrow from
  # iri_2020 which has them.
  for f in /tmp/fx/iri_2020/ccir*.asc /tmp/fx/iri_2020/ursi*.asc; do
    [ -f "$f" ] || continue
    [ -f "$FDIR/$(basename $f)" ] || cp "$f" "$FDIR/"
    [ -f "$CDIR/$(basename $f)" ] || cp "$f" "$CDIR/"
  done

  # Fortran build (prefer corpus_fixes for files patched there)
  SRCS_FX=""
  for f in "$SRCDIR"/*.for; do
    base=$(basename "$f")
    # Skip alternate program drivers that would duplicate main.
    case "$base" in
      *-test.for|imaztest.for|iriorbit.for|iriorbitmax.for) continue ;;
    esac
    rel="${f#/tmp/fx/}"
    fixed="$FIX/$rel"
    if [ -f "$fixed" ]; then
      SRCS_FX="$SRCS_FX $fixed"
    else
      SRCS_FX="$SRCS_FX $f"
    fi
  done
  SRCS="$SRCS_FX"
  echo "=== $NAME ==="
  echo "  Fortran build..."
  gfortran -O0 -std=legacy -fno-automatic -w -o "$FDIR/iritest" $SRCS 2>&1 | grep -iE "error" | head -3
  if [ ! -x "$FDIR/iritest" ]; then echo "  Fortran build FAILED"; return; fi

  # Run Fortran (cd into FDIR so the data files are in cwd)
  rm -f "$FDIR/fort.7"
  (cd "$FDIR" && echo "$IN" | timeout 60 ./iritest >/dev/null 2>stderr)
  if [ ! -f "$FDIR/fort.7" ]; then
    echo "  Fortran no fort.7 (stderr: $(head -1 $FDIR/stderr))"
    return
  fi

  # C++ build
  cd /home/user/llvm-project/flang/tools/fortran-to-cpp
  PYTHONPATH="$PWD:../flang-ast-py" FLANG=/home/user/llvm-project/build/bin/flang python3 -c "
import glob,os
from converter.project import convert_files
from pathlib import Path
FIX='${FIX}'
def fx(f):
    rel=os.path.relpath(f,'/tmp/fx'); p=os.path.join(FIX,rel); return p if os.path.exists(p) else f
files=[fx(f) for f in sorted(glob.glob('${SRCDIR}/*.for')) if 'test' not in os.path.basename(f) or '${DRIVER}' in os.path.basename(f)]
# remove standalone test programs (keep ${DRIVER} only)
_skip = ('imaztest.for','iriorbit.for','iriorbitmax.for')
files = [f for f in files if not os.path.basename(f).endswith('-test.for') and os.path.basename(f) not in _skip]
try:
    res=convert_files(files, flang=os.environ['FLANG'])
    for n,t in res.items():
        nm=os.path.basename(str(n))
        if not nm.endswith(('.cpp','.hpp')): nm=os.path.splitext(nm)[0]+'.cpp'
        open('${CDIR}/'+nm,'w').write(t)
    print('  C++ convert OK')
except Exception as e:
    print(f'  C++ convert FAILED: {e}')
" 2>&1 | tail -3

  g++ -std=c++20 -O0 -I "$RT" -I "$CDIR" "$CDIR"/*.cpp -o "$CDIR/iritest" 2>"$CDIR/build.err"
  if [ ! -x "$CDIR/iritest" ]; then
    echo "  C++ build FAILED"
    head -3 "$CDIR/build.err"
    return
  fi
  rm -f "$CDIR/fort.7"
  cd "$CDIR" && echo "$IN" | timeout 60 ./iritest >/dev/null 2>"$CDIR/stderr" && cd -
  if [ ! -f "$CDIR/fort.7" ]; then
    echo "  C++ no fort.7"
    return
  fi

  # Compare
  if cmp -s "$FDIR/fort.7" "$CDIR/fort.7"; then
    echo "  ✓ EXACT MATCH ($(wc -l <"$FDIR/fort.7") lines)"
  else
    NDIFF=$(diff "$FDIR/fort.7" "$CDIR/fort.7" | grep -c "^[<>]")
    echo "  ✗ DIFFERS ($NDIFF lines differ)"
    diff "$FDIR/fort.7" "$CDIR/fort.7" | head -8
  fi
}

run_version "iri2001" "/tmp/fx/iri_2001" "$INPUT_2001"
run_version "iri2007" "/tmp/fx/iri_2007" "$INPUT_2007"
run_version "iri2012" "/tmp/fx/iri_2012" "$INPUT_2012"
run_version "iri2016" "/tmp/fx/iri_2016" "$INPUT_2020"
run_version "iri2020" "/tmp/fx/iri_2020" "$INPUT_2020"
