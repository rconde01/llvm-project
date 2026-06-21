!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! Module / submodule and polymorphism facts:
!
!   module                 on a module-scope Name (ModuleDetails)
!   submodule              + parent_module on a SUBMODULE-scope Name
!   polymorphic            on CLASS(t) / CLASS(*) / TYPE(*) typed Names
!   unlimited_polymorphic  on CLASS(*) / TYPE(*) Names only

module base
  implicit none

  type :: pt
    real :: x, y
  end type
contains
  subroutine show_any(arg)
    class(*), intent(in) :: arg
    select type (arg)
    type is (integer);  print *, arg
    type is (real);     print *, arg
    class default;      print *, "unknown"
    end select
  end subroutine

  subroutine show_pt(p)
    class(pt), intent(in) :: p
    print *, p%x, p%y
  end subroutine
end module

submodule (base) child
  implicit none
contains
  subroutine helper
    print *, "helper"
  end subroutine
end submodule

! ----------------------------------------------------------------------
! ``base`` is a module: module:true.  No submodule fields.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"base"
! CHECK-DAG: "module":true

! ----------------------------------------------------------------------
! ``child`` is a submodule of ``base``: submodule:true and
! parent_module:"base" together.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"child"
! CHECK-DAG: "submodule":true
! CHECK-DAG: "parent_module":"base"

! ----------------------------------------------------------------------
! ``arg`` is CLASS(*) -- polymorphic and unlimited_polymorphic.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"arg"
! CHECK-DAG: "type":"CLASS(*)","polymorphic":true,"unlimited_polymorphic":true

! ----------------------------------------------------------------------
! ``p`` is CLASS(pt) -- polymorphic, not unlimited.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"p"
! CHECK-DAG: "type":"CLASS(pt)","polymorphic":true,"rank":0
