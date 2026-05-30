#!/usr/bin/env python3
"""
fix_trailing_return_void.py

Companion pass for fix-trailing-return.sh.

clang-tidy's `modernize-use-trailing-return-type` check has a long-standing
limitation: it silently skips functions whose return type is `void`. This
script fills that gap using libclang's AST, so the rewrite is correct rather
than regex-guessed:

    void  foo() const noexcept override;
        -> auto foo() const noexcept -> void override;

It deliberately handles ONLY plain `void` returns. Everything else is left to
clang-tidy. Safety properties:

  * Uses the real parsed AST, so `void*` returns, `(void)` parameters,
    `static_cast<void>(...)`, function-pointer typedefs, constructors,
    destructors and conversion operators are never touched (none of them have
    a standalone leading `void` return-type token).
  * `-> void` is inserted after the parameter list and its cv / ref / noexcept
    qualifiers but before `override` / `final` / `= 0` / the body, matching the
    C++ grammar.
  * Each edit is verified against the on-disk bytes before being applied; if
    the bytes don't match the expected token the function is skipped rather
    than risk corruption.

Usage (normally invoked by fix-trailing-return.sh, but standalone too):
    fix_trailing_return_void.py [--dry-run] [--std c++17]
                                [-I dir]... [-p compile-db-dir]
                                FILE [FILE...]
"""

import argparse
import sys

try:
    import clang.cindex as cx
except ImportError:
    sys.stderr.write(
        "error: python clang bindings not found. Install with "
        "`pip install libclang`.\n"
    )
    sys.exit(3)


# Cursor kinds that can legitimately carry a written `void` return type.
_FUNC_KINDS = {
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.CXX_METHOD,
    cx.CursorKind.FUNCTION_TEMPLATE,
}

# Tokens that may appear between the parameter list `)` and the trailing
# return type, and so must be kept *before* the inserted `-> void`.
_CV_REF = {"const", "volatile", "&", "&&"}


def _matching_close(tokens, open_idx):
    """Given index of a '(' token, return index of its matching ')'."""
    depth = 0
    for i in range(open_idx, len(tokens)):
        s = tokens[i].spelling
        if s == "(":
            depth += 1
        elif s == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _find_param_parens(cursor, tokens, void_idx):
    """Return (open_idx, close_idx) for the parameter list, or None."""
    params = [c for c in cursor.get_children()
              if c.kind == cx.CursorKind.PARM_DECL]

    if params:
        # Robust for operators too: locate the open paren enclosing the first
        # parameter by tracking paren depth.
        first_param_off = params[0].extent.start.offset
        stack = []
        for i, t in enumerate(tokens):
            s = t.spelling
            if s == "(":
                stack.append(i)
            elif s == ")":
                if stack:
                    stack.pop()
            if t.extent.start.offset >= first_param_off and stack:
                open_idx = stack[0]
                close_idx = _matching_close(tokens, open_idx)
                return (open_idx, close_idx) if close_idx else None
        return None

    # Zero parameters: the param list is the first top-level '(' after the
    # leading `void` token. Bracket operators (operator()/operator[]) put parens
    # in the name, so we skip those rare cases to stay safe.
    if cursor.spelling in ("operator()", "operator[]"):
        return None
    for i in range(void_idx + 1, len(tokens)):
        if tokens[i].spelling == "(":
            close_idx = _matching_close(tokens, i)
            return (i, close_idx) if close_idx else None
    return None


def _consume_balanced(tokens, i):
    """If tokens[i] is '(', skip to after the matching ')'. Return new index."""
    if i < len(tokens) and tokens[i].spelling == "(":
        close = _matching_close(tokens, i)
        if close is not None:
            return close + 1
    return i


def _insertion_offset(tokens, close_idx):
    """Byte offset after param-list qualifiers, where `-> void` should go."""
    last_end = tokens[close_idx].extent.end.offset
    i = close_idx + 1
    n = len(tokens)
    while i < n:
        s = tokens[i].spelling
        if s in _CV_REF:
            last_end = tokens[i].extent.end.offset
            i += 1
        elif s == "noexcept":
            last_end = tokens[i].extent.end.offset
            i += 1
            j = _consume_balanced(tokens, i)
            if j != i:
                last_end = tokens[j - 1].extent.end.offset
                i = j
        elif s == "throw":
            last_end = tokens[i].extent.end.offset
            i += 1
            j = _consume_balanced(tokens, i)
            if j != i:
                last_end = tokens[j - 1].extent.end.offset
                i = j
        elif s == "__attribute__":
            last_end = tokens[i].extent.end.offset
            i += 1
            j = _consume_balanced(tokens, i)  # handles the double parens too
            if j != i:
                last_end = tokens[j - 1].extent.end.offset
                i = j
        elif s == "[" and i + 1 < n and tokens[i + 1].spelling == "[":
            # C++ attribute [[ ... ]]
            depth = 0
            j = i
            while j < n:
                if tokens[j].spelling == "[":
                    depth += 1
                elif tokens[j].spelling == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            last_end = tokens[j].extent.end.offset if j < n else last_end
            i = j + 1
        else:
            # override / final / = / { / ; / anything else -> stop here.
            break
    return last_end


def collect_edits(cursor, path, data, edits, seen):
    """Walk the AST and record (offset, old_len, replacement) edits for path."""
    for node in cursor.walk_preorder():
        if node.kind not in _FUNC_KINDS:
            continue
        if node.result_type.spelling != "void":
            continue
        loc = node.location
        if loc.file is None or loc.file.name != path:
            continue

        tokens = list(node.get_tokens())
        if not tokens:
            continue

        # Leading `void` return token = first top-level 'void'.
        depth = 0
        void_idx = None
        for i, t in enumerate(tokens):
            s = t.spelling
            if s == "(":
                depth += 1
            elif s == ")":
                depth -= 1
            elif s == "void" and depth == 0:
                void_idx = i
                break
        if void_idx is None:
            continue  # already trailing, or void comes from a typedef/macro

        parens = _find_param_parens(node, tokens, void_idx)
        if parens is None:
            continue
        open_idx, close_idx = parens
        if not (void_idx < open_idx):
            continue  # the 'void' we found is after the params (trailing form)

        void_off = tokens[void_idx].extent.start.offset
        if void_off in seen:
            continue  # template + instantiation can revisit the same source
        # Verify the on-disk bytes really are `void` before editing.
        if data[void_off:void_off + 4] != b"void":
            continue
        seen.add(void_off)

        ins_off = _insertion_offset(tokens, close_idx)
        edits.append((void_off, 4, b"auto"))          # void -> auto
        edits.append((ins_off, 0, b" -> void"))        # insert trailing type


def process_file(path, args, index, comp_db):
    with open(path, "rb") as f:
        data = f.read()

    clang_args = ["-x", "c++", "-std=" + args.std]
    if comp_db is not None:
        cmds = comp_db.getCompileCommands(path)
        if cmds:
            # Drop the compiler argv[0] and the filename; keep the flags.
            raw = list(cmds[0].arguments)
            clang_args = [a for a in raw[1:] if a != path]
    for inc in args.include or []:
        clang_args.append("-I" + inc)

    tu = index.parse(path, args=clang_args,
                     options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

    edits = []
    seen = set()
    collect_edits(tu.cursor, path, data, edits, seen)
    if not edits:
        return 0

    n = len(edits) // 2
    if args.dry_run:
        for off, _old, _rep in sorted(e for e in edits if e[2] == b"auto"):
            line = data[:off].count(b"\n") + 1
            print(f"{path}:{line}: would add trailing return type (void)")
        return n

    # Apply highest offset first so earlier offsets stay valid.
    for off, old_len, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        data = data[:off] + rep + data[off + old_len:]
    with open(path, "wb") as f:
        f.write(data)
    return n


def main():
    ap = argparse.ArgumentParser(description="Add trailing return types to "
                                             "void-returning functions.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("-n", "--dry-run", action="store_true")
    ap.add_argument("-s", "--std", default="c++17")
    ap.add_argument("-I", "--include", action="append", default=[])
    ap.add_argument("-p", "--compile-db")
    args = ap.parse_args()

    index = cx.Index.create()
    comp_db = None
    if args.compile_db:
        try:
            comp_db = cx.CompilationDatabase.fromDirectory(args.compile_db)
        except cx.CompilationDatabaseError:
            sys.stderr.write(
                f"warning: could not load compile DB from {args.compile_db}; "
                "falling back to --std flags.\n")

    total = 0
    for path in args.files:
        try:
            total += process_file(path, args, index, comp_db)
        except Exception as exc:  # keep going across the tree
            sys.stderr.write(f"warning: skipped {path}: {exc}\n")
    verb = "would convert" if args.dry_run else "converted"
    print(f"void-pass: {verb} {total} function(s).")


if __name__ == "__main__":
    main()
