set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})

add_flang_nongtest_unittest(leading-zero-bit-count
  NonGTestTesting
)

add_flang_nongtest_unittest(bit-population-count
  NonGTestTesting
)

add_flang_nongtest_unittest(uint128
  NonGTestTesting
)

add_flang_nongtest_unittest(expression
  FortranSupport
  NonGTestTesting
  FortranEvaluate
  ${FORTRAN_SEMANTIC_LIBS}
  FortranParser
)

add_flang_nongtest_unittest(integer
  NonGTestTesting
  FortranEvaluate
  ${FORTRAN_SEMANTIC_LIBS}
)

add_flang_nongtest_unittest(intrinsics
  FortranSupport
  NonGTestTesting
  FortranEvaluate
  FortranDecimal
  ${FORTRAN_SEMANTIC_LIBS}
  FortranParser
)

add_flang_nongtest_unittest(logical
  NonGTestTesting
  FortranEvaluate
  ${FORTRAN_SEMANTIC_LIBS}
)

# GCC -fno-exceptions breaks the fenv.h interfaces needed to capture
# IEEE exception flags (different use of the word "exception")
# in the actual hardware floating-point status register, so ensure that
# C++ exceptions are enabled for this test.
set(LLVM_REQUIRES_EH ON)
set(LLVM_REQUIRES_RTTI ON)
add_flang_nongtest_unittest(real
  NonGTestTesting
  FortranEvaluate
  FortranDecimal
  ${FORTRAN_SEMANTIC_LIBS}
)
llvm_update_compile_flags(real.test)

add_flang_nongtest_unittest(folding
  FortranSupport
  NonGTestTesting
  FortranEvaluate
  ${FORTRAN_SEMANTIC_LIBS}
)
