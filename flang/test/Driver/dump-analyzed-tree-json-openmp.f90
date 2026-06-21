!----------
! RUN lines
!----------
! RUN: %flang_fc1 -fopenmp -fdebug-dump-analyzed-tree-json %s 2>&1 \
! RUN:   | FileCheck %s

! Smoke test for OpenMP directives.  The dumper's generic Pre/Post walks
! every parse-tree node, so any OpenMP construct should produce its
! corresponding parse-tree class as a JSON node without crashing or
! emitting malformed JSON.  We pin a handful of construct kinds to
! confirm coverage.

program p
  integer :: i, n
  real    :: a(100), s
  n = 100
  s = 0.0
  !$omp parallel do reduction(+:s)
  do i = 1, n
     a(i) = real(i)
     s    = s + a(i)
  end do
  !$omp end parallel do
  print *, s
end program

! ----------------------------------------------------------------------
! The OpenMP-block-construct and its components produce parse-tree nodes.
! ----------------------------------------------------------------------
! CHECK-DAG: "kind":"OpenMPConstruct"
! CHECK-DAG: "kind":"OpenMPLoopConstruct"
! CHECK-DAG: "kind":"OmpBeginDirective"
! CHECK-DAG: "kind":"OmpClause"
! CHECK-DAG: "kind":"OmpReductionClause"

! The directive's reduction clause carries the reduction variable's name
! as a regular Name node -- so type / attrs / etc. are available exactly
! as they are anywhere else.
! CHECK-DAG: "fortran":"s","type":"REAL(4)"

! The OmpDirectiveName node surfaces the construct's directive string
! (``parallel do``); each OmpClause carries the clause-name discriminant
! (``reduction``).  Lets a consumer identify constructs and clauses
! without consulting an OpenMP-version table.
! CHECK-DAG: "directive":"parallel do"
! CHECK-DAG: "clause":"reduction"
