!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! The JSON dumper emits each Name's resolved-symbol attribute set as a
! lowercase JSON string array under the "attrs" key.  Both the on-
! declaration form ("real, intent(in) :: x") and the standalone-statement
! form ("intent(in) :: x" after a separate "real :: x") consolidate onto
! the same symbol, so both produce identical "attrs".

subroutine s1(a, b, c, d)
  real, intent(in)    :: a
  real, optional      :: b
  real, intent(inout) :: c
  real                :: d
  intent(out) :: d        ! standalone INTENT statement
  d = a + c
  if (present(b)) d = d + b
end subroutine

subroutine s2(p, q)
  real, pointer       :: p
  integer, allocatable, target :: q(:)
end subroutine

! CHECK: "fortran":"a"
! CHECK-SAME: "attrs":["intent(in)"]
! CHECK: "fortran":"b"
! CHECK-SAME: "attrs":["optional"]
! CHECK: "fortran":"c"
! CHECK-SAME: "attrs":["intent(inout)"]
! CHECK: "fortran":"d"
! CHECK-SAME: "attrs":["intent(out)"]

! CHECK: "fortran":"p"
! CHECK-SAME: "attrs":["pointer"]
! CHECK: "fortran":"q"
! CHECK-SAME: "attrs":["allocatable","target"]
