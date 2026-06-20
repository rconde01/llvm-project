!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! A breadth-only smoke test.  One program that exercises many construct
! families at once -- module + derived type + generic + subroutine +
! WHERE / SELECT CASE / DO / IF + array section + named constant -- so
! the dumper sees, in a single invocation, the shapes a downstream tool
! is likeliest to encounter.  We only spot-check the semantic facts
! that distinguish this dumper from -fdebug-dump-parse-tree.  Order is
! tested with CHECK-DAG (any-order, must-all-appear) so this stays
! tolerant of parse-tree shape evolution.

module m
  implicit none
  integer, parameter :: nrow = 4

  type :: pt
     real :: x
     real :: y
  end type pt

  interface area
     module procedure area_real
     module procedure area_pt
  end interface
contains

  pure function area_real(w, h) result(a)
    real, intent(in) :: w, h
    real :: a
    a = w * h
  end function area_real

  pure function area_pt(p) result(a)
    type(pt), intent(in) :: p
    real :: a
    a = p%x * p%y
  end function area_pt

end module m

subroutine demo(n, v, mask, label)
  use m, only : nrow, pt, area
  integer, intent(in)              :: n
  real,    intent(inout)           :: v(nrow)
  logical, intent(in)              :: mask(nrow)
  character(len=8), intent(in)     :: label
  type(pt)                         :: q
  integer                          :: i

  where (mask)
     v(:) = v(:) * 2.0
  end where

  do i = 1, nrow
     if (v(i) > 0.0) then
        q = pt(real(i), v(i))
        v(i) = area(q)
     end if
  end do

  select case (n)
  case (:0)
     v(1) = 0.0
  case (1:nrow)
     v(n) = v(n) + 1.0
  case default
     v(nrow) = -1.0
  end select
end subroutine demo

! ----------------------------------------------------------------------
! Construct families: parse-tree node kinds for the constructs above
! all appear, in any JSON order.
! ----------------------------------------------------------------------
! CHECK-DAG: "kind":"Module"
! CHECK-DAG: "kind":"DerivedTypeDef"
! CHECK-DAG: "kind":"InterfaceBlock"
! CHECK-DAG: "kind":"WhereConstruct"
! CHECK-DAG: "kind":"NonLabelDoStmt"
! CHECK-DAG: "kind":"IfThenStmt"
! CHECK-DAG: "kind":"CaseConstruct"

! ----------------------------------------------------------------------
! Module-level PARAMETER 'nrow' resolves as INTEGER(4), rank 0, with
! the PARAMETER attribute among its attrs.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"nrow","type":"INTEGER(4)","rank":0,"object":true,"attrs":["parameter"{{[^]]*}}]

! The folded value 4 carries category / value on the analyzed Expr.
! CHECK-DAG: "type":"INTEGER(4)","rank":0,"category":"constant","value":"4"

! ----------------------------------------------------------------------
! Subroutine dummies survive resolution.
!   v    -> rank-1 REAL(4) with intent(inout), constant-foldable shape
!   mask -> rank-1 LOGICAL(4) with intent(in)
!   label -> CHARACTER, intent(in)
!   n    -> scalar INTEGER, intent(in)
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"v","type":"REAL(4)","rank":1,"shape":{{\[\[1,4\]\]}},"object":true,"attrs":["intent(inout)"]
! CHECK-DAG: "fortran":"mask","type":"LOGICAL(4)","rank":1,"shape":{{\[\[1,4\]\]}},"object":true,"attrs":["intent(in)"]
! CHECK-DAG: "fortran":"label","type":"CHARACTER(8_4,1)","rank":0,"object":true,"attrs":["intent(in)"]
! CHECK-DAG: "fortran":"n","type":"INTEGER(4)","rank":0,"object":true,"attrs":["intent(in)"]

! ----------------------------------------------------------------------
! USE-association: any nrow Name resolved through the use statement
! carries "assoc":"use" -- a downstream tool can tell host-only names
! apart from module-imported ones.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"nrow"{{[^}]*}}"assoc":"use"
