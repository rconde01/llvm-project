!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-analyzed-tree-json %s 2>&1 | FileCheck %s

! Four resolved-symbol layout / linkage facts the dumper emits on each
! Name that the analyzer computed:
!
!   size              storage size in bytes (when nonzero)
!   offset            byte offset within the enclosing aggregate -- COMMON
!                     block, EQUIVALENCE buffer, or derived type
!   from_module       the source module's name for a use-associated Name
!   bind_name         the BIND(C, NAME="...") C linkage name
!
! Plus the COMMON-block-name symbol's ``common_block_layout`` summary:
! ordered members with name / size / offset and the block's declared
! alignment.  Lets a binary-tooling consumer reconstruct the block
! layout from the dumper's one-shot output.

module mm
  implicit none
  integer :: shared_int
contains
  subroutine c_helper(x) bind(C, name="c_helper_impl")
    integer, intent(in) :: x
  end subroutine
end module

subroutine demo
  use mm, only: shared_int
  real    :: a, b
  integer :: i
  common /blk/ a, b, i
  shared_int = 42
  a = 1.0
end subroutine

! ----------------------------------------------------------------------
! shared_int is use-associated from module mm; its Name carries
! "from_module":"mm" so a tool can resolve the import without walking
! the USE statement.  ``assoc:"use"`` and ``from_module`` both appear
! somewhere in the dump.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"shared_int"
! CHECK-DAG: "assoc":"use"
! CHECK-DAG: "from_module":"mm"

! ----------------------------------------------------------------------
! c_helper is BIND(C, NAME="c_helper_impl"); the JSON exposes the C
! linkage name verbatim.
! ----------------------------------------------------------------------
! CHECK-DAG: "bind_name":"c_helper_impl"

! ----------------------------------------------------------------------
! Members of COMMON /blk/ carry size and offset individually.  ``a`` is
! at offset 0 (so the ``offset`` field is omitted as the value is the
! default zero), ``b`` is at offset 4, ``i`` is at offset 8.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"a","type":"REAL(4)","rank":0,"object":true,"common_block":"blk","size":4}
! CHECK-DAG: "fortran":"b","type":"REAL(4)","rank":0,"object":true,"common_block":"blk","size":4,"offset":4}
! CHECK-DAG: "fortran":"i","type":"INTEGER(4)","rank":0,"object":true,"common_block":"blk","size":4,"offset":8}

! ----------------------------------------------------------------------
! And the /blk/ block-name symbol carries a ``common_block_layout``
! sub-object summarizing the whole block: alignment plus an objects
! array with one {name, size, offset?} entry per member in declared
! order.
! ----------------------------------------------------------------------
! CHECK-DAG: "fortran":"blk","rank":0,"size":12,"common_block_layout":{"alignment":4,"objects":{{\[}}{"name":"a","size":4},{"name":"b","size":4,"offset":4},{"name":"i","size":4,"offset":8}]}
