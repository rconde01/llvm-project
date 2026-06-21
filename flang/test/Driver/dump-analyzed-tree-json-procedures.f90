!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! Per-symbol facts that summarize structure other than scalar layout:
!
!   procedure        on a SUBROUTINE/FUNCTION symbol's Name, an object
!                    carrying is_function plus the ordered dummy_args
!                    array and (for functions) a result.  Each
!                    dummy/result carries name, type, rank, and the
!                    intent / optional / pointer / etc. attrs.
!
!   components       on a derived-type-name symbol, the ordered
!                    component list with per-component type, rank,
!                    size, and offset.
!
!   generic          on a generic-interface symbol, the kind and the
!                    list of specific procedures.
!
!   from_name        on a use-associated Name renamed at the USE site,
!                    the originating module-symbol's source name.

module shapes
  implicit none

  type :: point
    real    :: x
    real    :: y
    integer :: tag
  end type point

  interface area
    module procedure area_real, area_int
  end interface

contains
  pure function area_real(r) result(a)
    real, intent(in) :: r
    real             :: a
    a = 3.14159 * r * r
  end function

  pure function area_int(r) result(a)
    integer, intent(in) :: r
    integer             :: a
    a = 3 * r * r
  end function

  subroutine translate(p, dx, dy)
    type(point), intent(inout) :: p
    real,        intent(in)    :: dx, dy
    p%x = p%x + dx
    p%y = p%y + dy
  end subroutine
end module

subroutine demo
  use shapes, only: pt => point, translate, area
  type(pt) :: q
  q%x = 0.0
  q%y = 0.0
  call translate(q, 1.0, 2.0)
  print *, area(2.0)
end subroutine

! ----------------------------------------------------------------------
! ``translate`` is a SUBROUTINE with three INTENT-bearing dummies; the
! signature carries is_function:false, dummy_args with name/type/rank/
! intent attrs, and no result.  Anchored on the literal procedure object
! so the test is robust to other Name fields appearing in between.
! ----------------------------------------------------------------------
! CHECK-DAG: "procedure":{"is_function":false,"dummy_args":[{"name":"p","type":"TYPE(point)","rank":0,"attrs":["intent(inout)"]},{"name":"dx","type":"REAL(4)","rank":0,"attrs":["intent(in)"]},{"name":"dy","type":"REAL(4)","rank":0,"attrs":["intent(in)"]}]}

! ----------------------------------------------------------------------
! ``area_real`` is a FUNCTION with one INTENT(IN) dummy and a REAL
! result.  is_function:true, dummy_args + result both present.
! ----------------------------------------------------------------------
! CHECK-DAG: "procedure":{"is_function":true,"dummy_args":[{"name":"r","type":"REAL(4)","rank":0,"attrs":["intent(in)"]}],"result":{"name":"a","type":"REAL(4)","rank":0}}

! ----------------------------------------------------------------------
! ``area_int`` mirrors area_real with integers, confirming the
! signature serialization is consistent across functions.
! ----------------------------------------------------------------------
! CHECK-DAG: "procedure":{"is_function":true,"dummy_args":[{"name":"r","type":"INTEGER(4)","rank":0,"attrs":["intent(in)"]}],"result":{"name":"a","type":"INTEGER(4)","rank":0}}

! ----------------------------------------------------------------------
! Derived type ``point`` exposes its ordered components with type/rank/
! offset/size.  x at offset 0 (omitted), y at offset 4, tag at offset 8.
! ----------------------------------------------------------------------
! CHECK-DAG: "components":[{"name":"x","type":"REAL(4)","rank":0,"size":4},{"name":"y","type":"REAL(4)","rank":0,"size":4,"offset":4},{"name":"tag","type":"INTEGER(4)","rank":0,"size":4,"offset":8}]

! ----------------------------------------------------------------------
! ``area`` is a generic-interface name; the generic field carries the
! kind plus the two specifics (the module procedures).
! ----------------------------------------------------------------------
! CHECK-DAG: "generic":{"kind":"Name","specifics":["area_real","area_int"]}

! ----------------------------------------------------------------------
! ``pt`` is renamed from ``point`` at the USE site; the dumper surfaces
! the source name (``point``) so a tool can map back without re-parsing
! the USE statement.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"pt"
! CHECK-DAG: "from_module":"shapes"
! CHECK-DAG: "from_name":"point"
