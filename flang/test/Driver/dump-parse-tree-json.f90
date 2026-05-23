!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-parse-tree-json %s 2>&1 | FileCheck %s --check-prefix=SEMA_ON
! RUN: %flang_fc1 -fdebug-dump-parse-tree-json-no-sema %s 2>&1 | FileCheck %s --check-prefix=SEMA_OFF

! Smoke test for the JSON parse tree dumper.  We do not pin the entire
! output; we just verify that the result is a single JSON object whose
! root node is "Program" and that a few interesting sub-nodes appear.

! SEMA_ON: {"kind":"Program"
! SEMA_ON-SAME: {"kind":"MainProgram"
! SEMA_ON-SAME: {"kind":"Name"
! SEMA_ON-SAME: "fortran":"main"

! SEMA_OFF: {"kind":"Program"
! SEMA_OFF-SAME: {"kind":"MainProgram"
! SEMA_OFF-SAME: {"kind":"Name"
! SEMA_OFF-SAME: "fortran":"main"

program main
  integer :: j
  j = 1
end program
