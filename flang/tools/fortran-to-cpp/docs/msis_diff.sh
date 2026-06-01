#!/bin/bash
# Run Fortran + C++ for each MSIS-family model, diff the outputs.
# Mirrors docs/iri_diff.sh but for the MSIS family
# (NRLMSISE-00, MSIS-86, MSIS-90).
set -u
RT=/home/user/llvm-project/flang/tools/fortran-to-cpp/runtime/include
FIX=/home/user/llvm-project/flang/tools/fortran-to-cpp/corpus_fixes

run_case() {
  local NAME="$1"; local SRCDIR="$2"; local SRCS="$3"; local INPUT="$4"
  local FDIR="/tmp/v/${NAME}_f" CDIR="/tmp/v/${NAME}_c"
  mkdir -p "$FDIR" "$CDIR"

  # data files (case-insensitive copy to UPPER-case for MSIS86)
  for f in "$SRCDIR"/*.dat "$SRCDIR"/*.DAT "$SRCDIR"/*.asc; do
    [ -f "$f" ] || continue
    cp "$f" "$FDIR/" 2>/dev/null
    cp "$f" "$CDIR/" 2>/dev/null
    upper=$(basename "$f" | tr '[:lower:]' '[:upper:]')
    cp "$f" "$FDIR/$upper" 2>/dev/null
    cp "$f" "$CDIR/$upper" 2>/dev/null
  done

  # Substitute corpus_fixes when available.
  SRCS_FX=""
  for f in $SRCS; do
    rel="${f#/tmp/fx/}"
    fixed="$FIX/$rel"
    if [ -f "$fixed" ]; then
      SRCS_FX="$SRCS_FX $fixed"
    else
      SRCS_FX="$SRCS_FX $f"
    fi
  done

  echo "=== $NAME ==="
  echo "  Fortran build..."
  gfortran -O0 -std=legacy -fno-automatic -w -o "$FDIR/run" $SRCS_FX 2>&1 | grep -iE "error" | head -3
  if [ ! -x "$FDIR/run" ]; then echo "  Fortran build FAILED"; return; fi

  rm -f "$FDIR/run.out"
  (cd "$FDIR" && echo "$INPUT" | timeout 60 ./run > run.out 2>stderr)

  # C++ build
  cd /home/user/llvm-project/flang/tools/fortran-to-cpp
  PYTHONPATH="$PWD:../flang-ast-py" FLANG=/home/user/llvm-project/build/bin/flang python3 -c "
import os
from converter.project import convert_files
files = $(printf "['%s'" "$(echo $SRCS_FX | awk '{print $1}')"; for f in $(echo $SRCS_FX | cut -d' ' -f2-); do printf ",'%s'" "$f"; done; printf "]")
try:
    res = convert_files(files, flang=os.environ['FLANG'])
    for n, t in res.items():
        nm = os.path.basename(str(n))
        if not nm.endswith(('.cpp', '.hpp')):
            nm = os.path.splitext(nm)[0] + '.cpp'
        open('${CDIR}/' + nm, 'w').write(t)
    print('  C++ convert OK')
except Exception as e:
    print(f'  C++ convert FAILED: {e}')
" 2>&1 | tail -3

  g++ -std=c++20 -ftemplate-depth=2048 -O0 -I "$RT" -I "$CDIR" "$CDIR"/*.cpp -o "$CDIR/run" 2>"$CDIR/build.err"
  if [ ! -x "$CDIR/run" ]; then
    echo "  C++ build FAILED"
    head -5 "$CDIR/build.err"
    return
  fi

  (cd "$CDIR" && echo "$INPUT" | timeout 60 ./run > run.out 2>"$CDIR/stderr")

  if cmp -s "$FDIR/run.out" "$CDIR/run.out"; then
    echo "  ✓ EXACT MATCH ($(wc -l <"$FDIR/run.out") lines)"
  else
    NDIFF=$(diff "$FDIR/run.out" "$CDIR/run.out" | grep -c "^[<>]")
    echo "  ✗ DIFFERS ($NDIFF lines differ)"
    diff "$FDIR/run.out" "$CDIR/run.out" | head -8
  fi
}

# NRLMSISE-00 / nrlmsis00 (driver + sub)
run_case "nrlmsis00" "/tmp/fx/nrlmsis00" \
  "/tmp/fx/nrlmsis00/nrlmsise00_sub.for /tmp/fx/nrlmsis00/nrlmsise00_driver.for" \
  ""

# MSIS-86 with m86tes (the test driver, non-interactive)
run_case "msis86_tes" "/tmp/fx/msis86" \
  "/tmp/fx/msis86/msis86.for /tmp/fx/msis86/m86tes.for" \
  ""
