!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! Per-Name fact blocks for declarations whose semantic content lives
! inside a less-common Details class:
!
!   binds_to            on a type-bound procedure binding Name
!                       (ProcBindingDetails), the implementation
!                       procedure's name.
!   namelist_objects    on a NAMELIST-group Name (NamelistDetails),
!                       the ordered object-name array.
!   assoc_expr          on an ASSOCIATE / SELECT TYPE construct
!                       entity Name (AssocEntityDetails), the
!                       analyzed Fortran rendering of the source
!                       expression.
!   init                on an Object whose declaration carried an
!                       initializer (REAL :: x = 1.5, PARAMETER
!                       :: pi = 3.14_4), the folded init value.

module m
  implicit none

  real, parameter :: pi = 3.14159_4
  integer         :: counter = 0

  type :: shape
  contains
    procedure, pass(self) :: describe => shape_describe
    final :: shape_finalize
  end type

  type :: legacy_layout
    sequence
    integer :: tag
    real    :: payload
  end type

  integer :: ia, ib, ic
  namelist /nl/ ia, ib, ic

contains
  subroutine shape_describe(self)
    class(shape), intent(in) :: self
    print *, "shape"
  end subroutine

  subroutine shape_finalize(self)
    type(shape), intent(inout) :: self
  end subroutine

  subroutine demo
    real :: r(10) = 0.0
    associate (s => sum(r))
      print *, s
    end associate
  end subroutine
end module

! ----------------------------------------------------------------------
! PARAMETER initializer: the folded literal lands on the symbol's
! ``init`` field directly (no need to walk Initialization).
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"pi"
! CHECK-DAG: "init":"3.141590118408203125_4"

! ----------------------------------------------------------------------
! Non-PARAMETER initializer for an ordinary variable also reaches
! init -- semantics has folded ``0`` to its INTEGER(4) form.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"counter"
! CHECK-DAG: "init":"0_4"

! ----------------------------------------------------------------------
! Type-bound procedure binding: ``describe`` resolves to
! ``shape_describe`` and carries the explicit PASS(self) target.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"describe"
! CHECK-DAG: "binds_to":"shape_describe"
! CHECK-DAG: "pass_name":"self"

! ----------------------------------------------------------------------
! The ``shape`` derived type has a FINAL subroutine attached.
! ``finals`` exposes the bound subprogram name in declaration order.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"shape"
! CHECK-DAG: "finals":["shape_finalize"]

! ----------------------------------------------------------------------
! A SEQUENCE-typed derived type carries ``sequence_type:true``.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"legacy_layout"
! CHECK-DAG: "sequence_type":true

! ----------------------------------------------------------------------
! NAMELIST group ``nl`` enumerates ia / ib / ic in declaration order.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"nl"
! CHECK-DAG: "namelist_objects":["ia","ib","ic"]

! ----------------------------------------------------------------------
! ASSOCIATE entity ``s`` carries the analyzed expression's Fortran
! rendering -- ``sum(r)`` over the rank-1 REAL array.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"s"
! CHECK-DAG: "assoc_expr":"sum(r)"
