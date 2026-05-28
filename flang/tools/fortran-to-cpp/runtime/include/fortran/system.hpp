//===-- fortran/system.hpp - Command-line / environment intrinsics ---*-C++-*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Implementations of the (de-facto standard, vendor) intrinsics that
// command-line tools use to reach the process environment:
//   IARGC / NARGS / GETARG  - command-line arguments
//   GETENVQQ / GET_ENVIRONMENT_VARIABLE - environment variables
//   SYSTEMQQ / SYSTEM       - run a shell command
//
// The command line is inherently process-global state, set once at start
// up from ``main``'s ``argc`` / ``argv`` and only read thereafter, so a
// process-wide store (not threaded through call sites) is the right model.
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_SYSTEM_HPP
#define FORTRAN_RT_SYSTEM_HPP

#include "string.hpp"

#include <cstdlib>
#include <string>
#include <string_view>

namespace fortran {

namespace detail {
inline int &argc_store() {
  static int a = 0;
  return a;
}
inline char **&argv_store() {
  static char **v = nullptr;
  return v;
}
// A character actual is blank-padded; trim trailing blanks for use as a
// C string (env-var name, shell command).
inline std::string trimmed_cstr(std::string_view s) {
  std::size_t n = s.size();
  while (n > 0 && s[n - 1] == ' ')
    --n;
  return std::string{s.substr(0, n)};
}
} // namespace detail

/// Record ``main``'s arguments so GETARG / IARGC / NARGS can serve them.
inline void set_command_args(int argc, char **argv) noexcept {
  detail::argc_store() = argc;
  detail::argv_store() = argv;
}

/// IARGC(): number of command-line arguments, excluding the program name.
inline int iargc() noexcept {
  const int a = detail::argc_store();
  return a > 0 ? a - 1 : 0;
}

/// NARGS(): Compaq/Intel extension — argument count *including* the program
/// name (so the user argument count is ``NARGS() - 1``).
inline int nargs() noexcept { return detail::argc_store(); }

/// GETARG(k, value[, status]): the k-th command argument (k = 0 is the
/// program name) copied into ``value`` with Fortran blank-pad / truncate.
/// ``status`` receives the argument length, or -1 when it is absent.
template <typename S>
void getarg(int k, S &&value, int &status) noexcept {
  const int a = detail::argc_store();
  char **v = detail::argv_store();
  if (v != nullptr && k >= 0 && k < a) {
    std::string_view s{v[k]};
    value = s;
    status = static_cast<int>(s.size());
  } else {
    value = std::string_view{""};
    status = -1;
  }
}
template <typename S> void getarg(int k, S &&value) noexcept {
  int status;
  getarg(k, static_cast<S &&>(value), status);
}

/// GET_COMMAND_ARGUMENT(k, value[, length[, status]]): the F2003 form.
template <typename S>
void get_command_argument(int k, S &&value, int &length, int &status) noexcept {
  getarg(k, static_cast<S &&>(value), length);
  status = length >= 0 ? 0 : 1;
}

/// GETENVQQ(name, value): set ``value`` to the named environment variable
/// and return its length (0 when unset).
template <typename N, typename S>
int getenvqq(const N &name, S &&value) noexcept {
  const std::string nm = detail::trimmed_cstr(std::string_view(name));
  if (const char *e = std::getenv(nm.c_str())) {
    std::string_view s{e};
    value = s;
    return static_cast<int>(s.size());
  }
  value = std::string_view{""};
  return 0;
}

/// SYSTEMQQ(command): run a shell command; true on success (exit 0).
template <typename S> bool systemqq(const S &command) noexcept {
  return std::system(detail::trimmed_cstr(std::string_view(command)).c_str()) ==
         0;
}

/// SYSTEM(command): the subroutine form; the exit status is ignored.
template <typename S> void system(const S &command) noexcept {
  (void)std::system(detail::trimmed_cstr(std::string_view(command)).c_str());
}

/// GETLASTERRORQQ(): last runtime error code — none is tracked.
inline int getlasterrorqq() noexcept { return 0; }

} // namespace fortran

#endif // FORTRAN_RT_SYSTEM_HPP
