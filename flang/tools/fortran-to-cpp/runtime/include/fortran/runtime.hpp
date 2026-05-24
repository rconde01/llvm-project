//===-- fortran/runtime.hpp - Umbrella header for the runtime ---*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Include everything from the fortran-to-cpp runtime in one go.  Pulled
// in by every generated translation unit; the individual headers stay
// usable in isolation for tests and finer-grained dependencies.
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_RUNTIME_HPP
#define FORTRAN_RT_RUNTIME_HPP

#include "fortran/array.hpp"
#include "fortran/array_ops.hpp"
#include "fortran/array_ref.hpp"
#include "fortran/equiv.hpp"
#include "fortran/intrinsics.hpp"
#include "fortran/io.hpp"
#include "fortran/string.hpp"
#include "fortran/time.hpp"

#endif // FORTRAN_RT_RUNTIME_HPP
