# Prebuilt flang binary

This orphan branch carries a stripped + xz-compressed prebuilt `flang-23`
binary (the AST-JSON-dumper-enabled build, the converter's frontend).

This avoids a ~5-minute rebuild after a container rollback resets the
git tree.

## Restore

```
git fetch origin flang-binary:flang-binary
git worktree add /tmp/fb flang-binary   # or `git checkout flang-binary` in a clean tree
bash /tmp/fb/tools/install-flang.sh
```

The script extracts into `build/bin/flang-23` and creates the `flang` symlink.

## Re-package after a rebuild

```
strip --strip-unneeded -o /tmp/flang-raw build/bin/flang-23
xz -9 -c /tmp/flang-raw > tools/flang-23.xz
git add tools/flang-23.xz && git commit -m "..." && git push
```
