# Standalone CMake build for the fortran-to-cpp runtime support library.
#
# This builds outside the LLVM tree as a header-only library with its
# own test executable.  Users of the converter's output include the
# headers directly; this CMake file is for developing and testing the
# runtime in isolation:
#
#   cd flang/tools/fortran-to-cpp/runtime
#   mkdir build && cd build
#   cmake .. && cmake --build . && ctest --output-on-failure

cmake_minimum_required(VERSION 3.20)
project(fortran_rt CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Header-only INTERFACE library so downstream code can do
# ``target_link_libraries(my_target PRIVATE fortran::runtime)``.
add_library(fortran_runtime INTERFACE)
add_library(fortran::runtime ALIAS fortran_runtime)
target_include_directories(fortran_runtime INTERFACE
  ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_features(fortran_runtime INTERFACE cxx_std_20)

enable_testing()

# Treat all warnings as errors for the tests so we catch regressions
# from header changes.
set(FORTRAN_RT_WARNINGS
  -Wall -Wextra -Wpedantic -Werror
  -Wno-unused-function -Wno-unused-variable)

function(add_rt_test name source)
  add_executable(${name} ${source})
  target_link_libraries(${name} PRIVATE fortran_runtime)
  target_include_directories(${name} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/tests)
  target_compile_options(${name} PRIVATE ${FORTRAN_RT_WARNINGS})
  add_test(NAME ${name} COMMAND ${name})
endfunction()

add_rt_test(test_array  tests/test_array.cpp)
add_rt_test(test_string tests/test_string.cpp)
add_rt_test(test_equiv  tests/test_equiv.cpp)
add_rt_test(test_io     tests/test_io.cpp)
