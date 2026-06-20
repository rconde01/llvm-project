!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json-no-sema %s 2>&1 | FileCheck %s

! The ``-no-sema`` action emits the same structural shape as the
! semantic-on dumper but skips semantic analysis.  Concretely, it omits
! every field that depends on resolved symbols or analyzed expressions:
! ``type``, ``rank``, ``shape``, ``object``, ``proc``, ``attrs``,
! ``assoc``, ``category``, and ``value`` must NOT appear; ``fortran`` on
! Name nodes carries the *parser-preserved* source spelling (with the
! original case) rather than the semantics-uppercased form.

program demo
  implicit none
  integer, intent(in) :: x   ! deliberately bogus to ensure semantics
                             ! would have rejected, but no-sema doesn't
                             ! run.
  x = 1
end program

! Structure is intact.
! CHECK: "kind":"Program"
! CHECK-SAME: "kind":"MainProgram"
! CHECK-SAME: "kind":"ProgramStmt"

! Name carries the original-case spelling (would be uppercased by sema).
! CHECK: "kind":"Name"
! CHECK-SAME: "fortran":"demo"

! ----------------------------------------------------------------------
! None of the semantic-only fields appear anywhere in the output.  The
! NOT checks scan the whole document.
! ----------------------------------------------------------------------
! CHECK-NOT: "type":"
! CHECK-NOT: "category":"
! CHECK-NOT: "value":"
! CHECK-NOT: "attrs":[
! CHECK-NOT: "object":true
! CHECK-NOT: "proc":true
! CHECK-NOT: "shape":[
! CHECK-NOT: "assoc":"
