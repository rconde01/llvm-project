//===-- test_string.cpp - Unit tests for FortranString ----------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "fortran/string.hpp"
#include "test_main.hpp"

#include <string>
#include <string_view>

using ftn::FortranString;
using namespace std::string_view_literals;

// ---- Default initialization & basic shape --------------------------------

TEST(default_is_all_blanks) {
  FortranString<5> s;
  CHECK_EQ(s.view(), "     "sv);
  CHECK_EQ(s.len_trim(), 0u);
  CHECK_EQ(s.trimmed(), ""sv);
}

TEST(length_is_part_of_the_type) {
  static_assert(FortranString<10>::length == 10);
  static_assert(FortranString<3>::length == 3);
  // The two types are distinct:
  static_assert(!std::is_same_v<FortranString<5>, FortranString<6>>);
}

// ---- Assignment: blank-pad and truncate ----------------------------------

TEST(shorter_string_is_blank_padded) {
  FortranString<10> name = "hi";
  CHECK_EQ(name.view(), "hi        "sv);
  CHECK_EQ(name.len_trim(), 2u);
}

TEST(longer_string_is_truncated) {
  FortranString<5> name = "hello world";
  CHECK_EQ(name.view(), "hello"sv);
  CHECK_EQ(name.len_trim(), 5u);
}

TEST(reassignment_overwrites_with_padding) {
  FortranString<8> s = "abcdef";
  CHECK_EQ(s.view(), "abcdef  "sv);
  s = "x";
  CHECK_EQ(s.view(), "x       "sv);
}

TEST(assign_from_different_length_fortran_string) {
  FortranString<6> a = "hello";
  FortranString<10> b = a;
  // a is "hello " (6 chars), b should be "hello     " (10 chars).
  CHECK_EQ(b.view(), "hello     "sv);

  FortranString<3> c = a;
  CHECK_EQ(c.view(), "hel"sv);
}

// ---- Equality is length-padded ------------------------------------------

TEST(equality_is_blank_padded) {
  FortranString<5> a = "hi";
  // a is "hi   " (5 chars).  Both operands should compare equal to
  // both "hi" and "hi   ".
  CHECK(a == "hi");
  CHECK(a == "hi   ");
  CHECK("hi" == a);
  CHECK(a != "hi!");
}

TEST(equality_between_different_lengths) {
  FortranString<4> a = "hi";   // "hi  "
  FortranString<10> b = "hi";  // "hi        "
  CHECK(a == b);
  CHECK(b == a);
}

TEST(ordering_uses_padded_comparison) {
  FortranString<5> a = "abc";
  FortranString<5> b = "abd";
  CHECK(a < b);
  CHECK(!(b < a));
  CHECK(a <= b);
  // "abc  " is greater than "ab" because of the third char.
  FortranString<5> c = "ab";
  CHECK(c < a);
}

// ---- Substring proxy ----------------------------------------------------

TEST(substring_read_view) {
  FortranString<10> s = "hello world";  // truncated to 10 -> "hello worl"
  CHECK_EQ(s.view(), "hello worl"sv);
  CHECK_EQ(static_cast<std::string_view>(s(1, 5)), "hello"sv);
  CHECK_EQ(static_cast<std::string_view>(s(7, 10)), "worl"sv);
}

TEST(substring_assignment_pads) {
  FortranString<10> s = "abcdefghij";
  s(3, 7) = "Z";              // 5-char slice, 1-char RHS -> "Z    "
  CHECK_EQ(s.view(), "abZ    hij"sv);
}

TEST(substring_assignment_truncates) {
  FortranString<10> s = "abcdefghij";
  s(2, 4) = "12345";          // 3-char slice, 5-char RHS -> takes "123"
  CHECK_EQ(s.view(), "a123efghij"sv);
}

TEST(substring_full_range_round_trip) {
  FortranString<5> s;
  s(1, 5) = "hello";
  CHECK_EQ(s.view(), "hello"sv);
}

// ---- Element indexing ---------------------------------------------------

TEST(element_indexing_is_one_based) {
  FortranString<5> s = "abcde";
  CHECK_EQ(s[1], 'a');
  CHECK_EQ(s[5], 'e');
  s[3] = 'X';
  CHECK_EQ(s.view(), "abXde"sv);
}

// ---- Concatenation -----------------------------------------------------

TEST(concatenation_returns_summed_length) {
  FortranString<3> a = "foo";
  FortranString<3> b = "bar";
  auto ab = a + b;
  static_assert(decltype(ab)::length == 6);
  CHECK_EQ(ab.view(), "foobar"sv);
}

TEST(concatenation_preserves_padding) {
  FortranString<5> a = "hi";       // "hi   "
  FortranString<3> b = "ab";       // "ab "
  auto ab = a + b;
  static_assert(decltype(ab)::length == 8);
  CHECK_EQ(ab.view(), "hi   ab "sv);
}

// ---- string_view implicit conversion ------------------------------------

TEST(implicit_conversion_to_string_view) {
  FortranString<5> s = "ok";
  std::string_view v = s;
  CHECK_EQ(v, "ok   "sv);
  CHECK_EQ(v.size(), 5u);
}

TEST(can_pass_to_function_taking_string_view) {
  auto check = [](std::string_view v) { return v.size(); };
  FortranString<7> s = "hi";
  CHECK_EQ(check(s), 7u);  // includes padding
  CHECK_EQ(check(s.trimmed()), 2u);
}

TEST(achar_and_ichar_round_trip) {
  auto a = ftn::achar(65); // 'A'
  CHECK(a == "A");
  CHECK_EQ(ftn::ichar("A"), 65);
  CHECK_EQ(ftn::ichar(ftn::achar(90)), 90); // 'Z'
  CHECK_EQ(ftn::ichar(std::string_view{}), 0);  // empty -> 0
}

TEST(repeat_concatenates) {
  CHECK(ftn::repeat("ab", 3) == "ababab");
  CHECK(ftn::repeat("x", 0).empty());
}

TEST(scan_finds_set_member) {
  CHECK_EQ(ftn::scan("hello", "l"), 3);
  CHECK_EQ(ftn::scan("hello", "l", /*back=*/true), 4);
  CHECK_EQ(ftn::scan("hello", "xyz"), 0);
}

TEST(verify_finds_non_member) {
  CHECK_EQ(ftn::verify("hello", "helo"), 0); // all chars in set
  CHECK_EQ(ftn::verify("hexlo", "helo"), 3); // 'x' not in set
  CHECK_EQ(ftn::verify("axbxc", "x", /*back=*/true), 5);
}

FORTRAN_RT_TEST_MAIN()
