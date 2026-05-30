//===-- test_main.hpp - Tiny zero-dependency test harness -------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Stand-alone test harness used by the fortran-to-cpp runtime test
// executables.  No external dependencies: defines a TEST(...) macro
// that registers a function, and a main() that runs them all and
// prints a summary.
//
// Usage:
//   #include "test_main.hpp"
//   TEST(name) { CHECK(some_condition()); }
//   FORTRAN_RT_TEST_MAIN()
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_TEST_MAIN_HPP
#define FORTRAN_RT_TEST_MAIN_HPP

#include <cstdio>
#include <exception>
#include <string>
#include <vector>

namespace fortran_rt_test {

struct TestEntry {
  const char *name;
  void (*fn)();
};

inline std::vector<TestEntry> &registry() {
  static std::vector<TestEntry> r;
  return r;
}

struct Registrar {
  Registrar(const char *name, void (*fn)()) {
    registry().push_back({name, fn});
  }
};

struct CheckFailure {
  std::string message;
};

inline void check_impl(bool cond, const char *expr, const char *file,
                       int line) {
  if (!cond) {
    char buf[512];
    std::snprintf(buf, sizeof(buf), "  CHECK failed: %s\n    at %s:%d", expr,
                  file, line);
    throw CheckFailure{buf};
  }
}

template <typename A, typename B>
inline void check_eq_impl(const A &a, const B &b, const char *aexpr,
                          const char *bexpr, const char *file, int line) {
  if (!(a == b)) {
    char buf[512];
    std::snprintf(buf, sizeof(buf),
                  "  CHECK_EQ failed: %s == %s\n    at %s:%d", aexpr, bexpr,
                  file, line);
    throw CheckFailure{buf};
  }
}

inline int run_all() {
  int passed = 0;
  int failed = 0;
  for (const auto &t : registry()) {
    try {
      t.fn();
      std::printf("  PASS  %s\n", t.name);
      ++passed;
    } catch (const CheckFailure &e) {
      std::printf("  FAIL  %s\n%s\n", t.name, e.message.c_str());
      ++failed;
    } catch (const std::exception &e) {
      std::printf("  FAIL  %s\n    unexpected exception: %s\n", t.name,
                  e.what());
      ++failed;
    }
  }
  std::printf("\n%d passed, %d failed (%zu total)\n", passed, failed,
              registry().size());
  return failed == 0 ? 0 : 1;
}

} // namespace fortran_rt_test

#define FORTRAN_RT_TEST_CAT_(a, b) a##b
#define FORTRAN_RT_TEST_CAT(a, b) FORTRAN_RT_TEST_CAT_(a, b)

#define TEST(NAME)                                                             \
  static void FORTRAN_RT_TEST_CAT(test_, NAME)();                              \
  static ::fortran_rt_test::Registrar FORTRAN_RT_TEST_CAT(reg_, NAME){         \
      #NAME, &FORTRAN_RT_TEST_CAT(test_, NAME)};                               \
  static void FORTRAN_RT_TEST_CAT(test_, NAME)()

#define CHECK(EXPR)                                                            \
  ::fortran_rt_test::check_impl(static_cast<bool>(EXPR), #EXPR, __FILE__,      \
                                __LINE__)

#define CHECK_EQ(A, B)                                                         \
  ::fortran_rt_test::check_eq_impl((A), (B), #A, #B, __FILE__, __LINE__)

#define CHECK_THROWS(EXPR, EXC)                                                \
  do {                                                                         \
    bool _ftr_threw = false;                                                   \
    try {                                                                      \
      (void)(EXPR);                                                            \
    } catch (const EXC &) {                                                    \
      _ftr_threw = true;                                                       \
    } catch (...) {                                                            \
    }                                                                          \
    if (!_ftr_threw) {                                                         \
      char _ftr_buf[256];                                                      \
      std::snprintf(_ftr_buf, sizeof(_ftr_buf),                                \
                    "  CHECK_THROWS failed: %s did not throw %s\n    at "      \
                    "%s:%d",                                                   \
                    #EXPR, #EXC, __FILE__, __LINE__);                          \
      throw ::fortran_rt_test::CheckFailure{_ftr_buf};                         \
    }                                                                          \
  } while (0)

#define FORTRAN_RT_TEST_MAIN()                                                 \
  int main() { return ::fortran_rt_test::run_all(); }

#endif // FORTRAN_RT_TEST_MAIN_HPP
