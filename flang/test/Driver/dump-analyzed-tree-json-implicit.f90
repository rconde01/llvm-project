!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! The `implicit` field flags Names whose symbol's type was set by
! IMPLICIT typing rules rather than an explicit declaration.  A
! reformatter that wants to add `IMPLICIT NONE` plus generated
! declarations can identify those names without re-running the
! implicit-typing pass.

! No IMPLICIT NONE here, so `j` (default INTEGER) and `x` (default REAL)
! are typed implicitly; `m` has an explicit declaration and is not.
subroutine s
  integer :: m
  m = 1
  j = 2 + m         ! j: implicit INTEGER
  x = real(m)       ! x: implicit REAL
end subroutine

! ----------------------------------------------------------------------
! Implicitly-typed names carry "implicit":true.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"j"{{[^}]*}}"implicit":true
! CHECK-DAG: "fortran":"x"{{[^}]*}}"implicit":true

! Explicitly-declared names do not.
! CHECK-NOT: "fortran":"m"{{[^}]*}}"implicit":true
