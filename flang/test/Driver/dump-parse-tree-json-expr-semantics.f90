!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fdebug-dump-parse-tree-json %s 2>&1 | FileCheck %s

! The JSON dumper emits resolved semantics on every parse-tree node that
! carries an analyzed expression — Expr, Variable, DataStmtConstant,
! AllocateObject, PointerObject.  Four fields are added next to the
! existing "fortran" string:
!
!   "type"      — the resolved type (e.g. "INTEGER(4)", "REAL(8)").
!   "rank"      — the expression's rank (0 for scalars).
!   "category"  — "variable"   (an assignable designator)
!               | "constant"   (folded compile-time value)
!               | "expression" (a computed value).
!   "value"     — the folded value as a decimal string, when the
!                 expression is a scalar integer constant.

program p
  integer, parameter :: n = 25
  integer :: a(n*n), b
  b = 1
  a(b+1) = a(b) + 2
end program

! Constant initializer "25" carries type, rank, category and folded value.
! CHECK: "type":"INTEGER(4)","rank":0,"category":"constant","value":"25"

! The array bound n*n folds to 625; the analyzed expression IS the
! folded constant, not the unevaluated product.
! CHECK: "type":"INTEGER(4)","rank":0,"category":"constant","value":"625"

! The LHS of "b = 1" is a Variable (an assignable designator).
! CHECK: "kind":"Variable"
! CHECK-SAME: "fortran":"b"
! CHECK-SAME: "category":"variable"

! ... and "1" on the RHS is a folded integer constant.
! CHECK: "type":"INTEGER(4)","rank":0,"category":"constant","value":"1"

! "b + 1" is an expression (computed) — no folded value.
! CHECK: "fortran":"b+1_4"
! CHECK-SAME: "category":"expression"

! "a(b) + 2" is also an expression.
! CHECK: "category":"expression"
