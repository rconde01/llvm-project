!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! Two things this test pins down:
!
!  1.  ``source`` is emitted as a sub-object with the source file's
!      absolute path, the verbatim source slice, and 1-based inclusive
!      ``line`` / ``col`` / ``endLine`` / ``endCol`` coordinates.
!
!  2.  A numeric statement label appears as the ``label`` field on the
!      enclosing ``Statement`` wrapper (the wrapper itself does carry
!      its own JSON node; only ``UnlabeledStatement`` is silent on
!      labels because there isn't one).

program p
  integer :: i

  i = 1
100 continue
  if (i < 10) goto 100
end program

! ----------------------------------------------------------------------
! The Name ``i`` in the declaration carries a one-character source
! range covering exactly that one column (the line number is implicit
! from the test file's layout, so we don't pin it).
! ----------------------------------------------------------------------
! CHECK: "kind":"Name"
! CHECK-SAME: "text":"i"
! CHECK-SAME: "col":14
! CHECK-SAME: "endCol":15

! ----------------------------------------------------------------------
! The labeled CONTINUE statement: ``label`` lives on the Statement
! wrapper, alongside the source range; the inner ContinueStmt itself is
! attribute-free.
! ----------------------------------------------------------------------
! CHECK: "kind":"Statement"
! CHECK-SAME: "text":"100 continue"
! CHECK-SAME: "label":100
! CHECK: "kind":"ContinueStmt"

! ----------------------------------------------------------------------
! The GOTO target appears as a uint64_t parse-tree leaf rendering the
! label number as ``fortran:"100"``.
! ----------------------------------------------------------------------
! CHECK: "kind":"GotoStmt"
! CHECK-SAME: "kind":"uint64_t"
! CHECK-SAME: "fortran":"100"
