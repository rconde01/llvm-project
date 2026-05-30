#!/usr/bin/env bash
#
# fix-trailing-return.sh
#
# Recursively find C/C++ source and header files under a root directory and
# rewrite function signatures that use the classic leading-return-type form
#
#     int  foo(double x);
#
# into the trailing-return-type form
#
#     auto foo(double x) -> int;
#
# This is done with clang-tidy's `modernize-use-trailing-return-type` check,
# which parses real C++ (so it won't corrupt templates, macros, pointers to
# functions, etc. the way a regex would).
#
# Usage:
#   ./fix-trailing-return.sh [options] <root-dir>
#
# Options:
#   -n, --dry-run        Report what would change; do not modify any files.
#   -s, --std <std>      C++ standard to parse with (default: c++17).
#   -p, --compile-db <d> Directory containing compile_commands.json. If given,
#                        clang-tidy uses the project's real compile flags.
#   -j, --jobs <n>       Parallel jobs (default: number of CPUs).
#   -I, --include <dir>  Extra include directory (repeatable). Ignored when a
#                        compile database is supplied.
#       --clang-tidy <p> Path to the clang-tidy binary (default: clang-tidy).
#   -h, --help           Show this help.
#
# Notes:
#   * Trailing return type requires C++11 or newer. Pure C files cannot use it,
#     so this only makes sense for C++ translation units / headers.
#   * Changes are applied in place. Run on a clean working tree (or use -n
#     first) so you can review the diff with your VCS afterwards.

set -euo pipefail

# ---- defaults ---------------------------------------------------------------
DRY_RUN=0
STD="c++17"
COMPILE_DB=""
JOBS="$(nproc 2>/dev/null || echo 4)"
CLANG_TIDY="clang-tidy"
ROOT=""
EXTRA_INCLUDES=()

CHECK="modernize-use-trailing-return-type"

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

# ---- argument parsing -------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run)    DRY_RUN=1; shift ;;
    -s|--std)        STD="$2"; shift 2 ;;
    -p|--compile-db) COMPILE_DB="$2"; shift 2 ;;
    -j|--jobs)       JOBS="$2"; shift 2 ;;
    -I|--include)    EXTRA_INCLUDES+=("$2"); shift 2 ;;
    --clang-tidy)    CLANG_TIDY="$2"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    -*)              echo "Unknown option: $1" >&2; usage; exit 2 ;;
    *)               if [[ -z "$ROOT" ]]; then ROOT="$1"; else
                       echo "Unexpected extra argument: $1" >&2; exit 2
                     fi; shift ;;
  esac
done

if [[ -z "$ROOT" ]]; then
  echo "Error: no root directory given." >&2
  usage; exit 2
fi
if [[ ! -d "$ROOT" ]]; then
  echo "Error: '$ROOT' is not a directory." >&2; exit 2
fi
if ! command -v "$CLANG_TIDY" >/dev/null 2>&1; then
  echo "Error: clang-tidy ('$CLANG_TIDY') not found in PATH." >&2; exit 2
fi

# ---- collect files ----------------------------------------------------------
# Header extensions (.h/.hpp/.hh/.hxx) and source extensions (.cpp/.cc/.cxx).
# .h is treated as C++ since the user asked for C++ trailing return types.
mapfile -d '' FILES < <(find "$ROOT" \
  -type f \( \
     -name '*.cpp' -o -name '*.cc' -o -name '*.cxx' -o -name '*.c++' -o \
     -name '*.hpp' -o -name '*.hh' -o -name '*.hxx' -o -name '*.h' \
  \) -print0)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "No C/C++ files found under '$ROOT'."; exit 0
fi
echo "Found ${#FILES[@]} file(s) under '$ROOT'."

# ---- build clang-tidy invocation -------------------------------------------
TIDY_ARGS=( "-checks=-*,${CHECK}" "--quiet" )
[[ $DRY_RUN -eq 0 ]] && TIDY_ARGS+=( "--fix" )

# Compiler flags passed after `--`. When a compile database exists we let
# clang-tidy read it instead, so we only build fallback flags here.
COMPILE_ARGS=( "-x" "c++" "-std=${STD}" )
for inc in "${EXTRA_INCLUDES[@]:-}"; do
  [[ -n "$inc" ]] && COMPILE_ARGS+=( "-I${inc}" )
done

if [[ -n "$COMPILE_DB" ]]; then
  if [[ ! -f "$COMPILE_DB/compile_commands.json" ]]; then
    echo "Error: no compile_commands.json in '$COMPILE_DB'." >&2; exit 2
  fi
  TIDY_ARGS+=( "-p" "$COMPILE_DB" )
  echo "Using compile database in '$COMPILE_DB'."
else
  echo "No compile database; parsing as ${STD} with minimal flags."
  echo "  (warnings about missing includes are harmless for this check)."
fi

[[ $DRY_RUN -eq 1 ]] && echo "DRY RUN: no files will be modified."

# ---- run, one file per process, in parallel --------------------------------
run_one() {
  local file="$1"; shift
  # Re-create arg arrays inside the subshell from the serialized env.
  # shellcheck disable=SC2206
  local tidy_args=( $TIDY_ARGS_STR )
  if [[ -n "$COMPILE_DB" ]]; then
    "$CLANG_TIDY" "${tidy_args[@]}" "$file" 2>/dev/null || true
  else
    # shellcheck disable=SC2206
    local compile_args=( $COMPILE_ARGS_STR )
    "$CLANG_TIDY" "${tidy_args[@]}" "$file" -- "${compile_args[@]}" 2>/dev/null || true
  fi
}
export -f run_one
export CLANG_TIDY COMPILE_DB
export TIDY_ARGS_STR="${TIDY_ARGS[*]}"
export COMPILE_ARGS_STR="${COMPILE_ARGS[*]}"

printf '%s\0' "${FILES[@]}" \
  | xargs -0 -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {}

echo "Done."
if [[ $DRY_RUN -eq 0 ]]; then
  echo "Review the changes with your version control (e.g. 'git diff')."
fi
