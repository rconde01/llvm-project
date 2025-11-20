set(FortranSemanticSources1 
  assignment.cpp
  attr.cpp
  canonicalize-acc.cpp
  canonicalize-directives.cpp
  canonicalize-do.cpp
  canonicalize-omp.cpp
  check-acc-structure.cpp
  check-allocate.cpp
  check-arithmeticif.cpp
  check-case.cpp
  check-coarray.cpp
  check-data.cpp
  check-deallocate.cpp
  check-declarations.cpp
  check-if-stmt.cpp
  check-io.cpp
  check-namelist.cpp
  check-nullify.cpp
  check-omp-atomic.cpp
  check-omp-loop.cpp
  check-omp-metadirective.cpp
  check-purity.cpp
  check-return.cpp
  check-select-rank.cpp
  check-select-type.cpp
  check-stop.cpp
  compute-offsets.cpp
  data-to-inits.cpp
  definable.cpp
  dump-expr.cpp
  mod-file.cpp
  openmp-dsa.cpp
  openmp-modifiers.cpp
  openmp-utils.cpp
  pointer-assignment.cpp
  program-tree.cpp
  resolve-directives.cpp
  resolve-labels.cpp
  resolve-names-utils.cpp
  rewrite-parse-tree.cpp
  runtime-type-info.cpp
  scope.cpp
  symbol.cpp
  tools.cpp
  type.cpp
  unparse-with-symbols.cpp
)

set(FortranSemanticSources2
  check-call.cpp
  check-cuda.cpp
  check-do-forall.cpp
  check-omp-structure.cpp
  expression.cpp
  resolve-names.cpp
  semantics.cpp
)

if(MSVC)
  add_flang_library(FortranSemantics1 ${FortranSemanticSources1} PARTIAL_SOURCES_INTENDED
    DEPENDS
    acc_gen
    omp_gen

    LINK_LIBS PUBLIC
    FortranSupport
    FortranParser
    FortranEvaluate

    LINK_COMPONENTS
    Support
    FrontendOpenMP
    FrontendOpenACC
    TargetParser
  )
  add_flang_library(FortranSemantics2 ${FortranSemanticSources2} PARTIAL_SOURCES_INTENDED
    DEPENDS
    acc_gen
    omp_gen

    LINK_LIBS PUBLIC
    FortranSupport
    FortranParser
    FortranEvaluate

    LINK_COMPONENTS
    Support
    FrontendOpenMP
    FrontendOpenACC
    TargetParser
  )

  target_precompile_headers(FortranSemantics2 PRIVATE
    [["flang/Semantics/semantics.h"]]
    [["flang/Semantics/type.h"]]
    [["flang/Semantics/openmp-modifiers.h"]]
    [["flang/Semantics/expression.h"]]
    [["flang/Semantics/tools.h"]]
    [["flang/Semantics/symbol.h"]]
  )
else()
  add_flang_library(FortranSemantics
    ${FortranSemanticSources1}
    ${FortranSemanticSources2}

    DEPENDS
    acc_gen
    omp_gen

    LINK_LIBS
    FortranSupport
    FortranParser
    FortranEvaluate

    LINK_COMPONENTS
    Support
    FrontendOpenMP
    FrontendOpenACC
    TargetParser
  )

  target_precompile_headers(FortranSemantics PRIVATE
    [["flang/Semantics/semantics.h"]]
    [["flang/Semantics/type.h"]]
    [["flang/Semantics/openmp-modifiers.h"]]
    [["flang/Semantics/expression.h"]]
    [["flang/Semantics/tools.h"]]
    [["flang/Semantics/symbol.h"]]
  )
endif()



