!----------
! RUN lines
!----------
! When semantics rejects a program, the analyzed-tree dumper does not
! emit a partial JSON document (flang stops after the failing semantic
! pass).  The companion no-sema action is the right tool for that use
! case -- IDE / source-rewriter consumers can still get the parse tree
! out of source semantics would have rejected.

! Both runs use a program with two intentional semantic errors:
!   1. ``a + b`` where ``b`` is undeclared under IMPLICIT NONE.
!   2. Assigning a LOGICAL to a CHARACTER variable (type mismatch).

! With sema enabled, flang exits non-zero with diagnostics.
! RUN: not %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 \
! RUN:   | FileCheck --check-prefix=SEMA_ERR %s

! With sema disabled, the same source dumps cleanly.
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json-no-sema %s 2>&1 \
! RUN:   | FileCheck --check-prefix=NO_SEMA %s

program bad
  implicit none
  integer :: a
  character(len=8) :: c
  a = a + b           ! 'b' is not declared
  c = .true.          ! type mismatch
end program

! ----------------------------------------------------------------------
! Sema-on path: report both diagnostics.
! ----------------------------------------------------------------------
! SEMA_ERR: error: No explicit type declared for 'b'
! SEMA_ERR: error: No intrinsic or user-defined ASSIGNMENT

! ----------------------------------------------------------------------
! No-sema path: a well-formed JSON document with the parse-tree
! structure preserved, regardless of the semantic errors -- the
! offending statements appear, just without resolved type / category
! fields.
! ----------------------------------------------------------------------
! NO_SEMA: {"kind":"Program"
! NO_SEMA-SAME: "kind":"MainProgram"
! NO_SEMA-SAME: "kind":"ProgramStmt"
! NO_SEMA: "kind":"AssignmentStmt"
! NO_SEMA-NOT: "type":"
! NO_SEMA-NOT: "category":"
