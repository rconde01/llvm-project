!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! Five resolved-symbol facts the dumper emits on each Name beyond the
! basic type/rank/attrs:
!
!   defined_at        the symbol's declaring source location
!   defined_in        owning derived-type name (for components)
!   common_block      COMMON-block name (for COMMON members)
!   equivalence_class 0-based index into the owning scope's
!                     equivalence-set list
!   proc_interface    explicit interface name (for procedure entities)
!   implicit          true on names typed by implicit-typing rules
!
! Plus value-rendering for non-integer scalar constants (REAL / LOGICAL
! / CHARACTER / COMPLEX) via the Fortran-source rendering of the folded
! expression.

module mm
  integer, parameter :: nn = 5

  type :: pt
     real :: x
     integer :: count
  end type
end module mm

subroutine demo
  use mm, only : nn, pt
  logical, parameter :: flag = .true.
  real,    parameter :: pi   = 3.14
  type(pt)                   :: q
  integer                    :: i
  real                       :: a
  integer                    :: ia
  common /blk/ i
  equivalence (a, ia)

  abstract interface
     subroutine cb(z)
       integer, intent(in) :: z
     end subroutine
  end interface
  procedure(cb), pointer :: callback

  i = nn
  q%x = pi
  a = 1.0
end subroutine demo

! ----------------------------------------------------------------------
! common_block: `i` lives in COMMON /blk/.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"i"{{[^}]*}}"common_block":"blk"

! ----------------------------------------------------------------------
! defined_in: the `x` of `q%x` is a component of derived type `pt`.
! Source-text inside the derived-type definition mentions `x` too, but
! that occurrence is the declaration itself -- on it ``defined_in``
! still appears (the symbol's owner is the type's scope).
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"x"{{[^}]*}}"defined_in":"pt"

! ----------------------------------------------------------------------
! equivalence_class: a and ia are co-aliased through EQUIVALENCE; they
! share the same 0-based index in the scope's equivalence-set list.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"a"{{[^}]*}}"equivalence_class":0
! CHECK-DAG: "fortran":"ia"{{[^}]*}}"equivalence_class":0

! ----------------------------------------------------------------------
! proc_interface: a procedure pointer's resolved interface is the
! abstract-interface name `cb`.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"callback"{{[^}]*}}"proc_interface":"cb"

! ----------------------------------------------------------------------
! defined_at: the use site of `nn` carries the declaration's source
! location.  The exact line / column depends on this test file's layout;
! we only pin the symbol-name text in the defined_at sub-object.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"nn"{{[^}]*}}"defined_at":{"text":"nn"

! ----------------------------------------------------------------------
! Folded non-integer scalar constants carry `value` as the Fortran
! rendering of the folded expression.
! ----------------------------------------------------------------------
! CHECK-DAG: "type":"LOGICAL(4)","rank":0,"category":"constant","value":".true._4"
! CHECK-DAG: "type":"REAL(4)","rank":0,"category":"constant","value":"3.{{[0-9]+}}_4"
