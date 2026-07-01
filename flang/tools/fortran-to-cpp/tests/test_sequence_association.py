"""Fortran sequence / storage association at call sites.

FORTRAN 77 lets an actual argument's storage be reinterpreted to match a
dummy of a different shape.  This file covers the *whole-array actual to a
scalar dummy* form: the dummy is storage-associated with the array's first
element, so the converter passes ``ftn::first(array)``.
"""

from __future__ import annotations

import unittest

from _support import convert_project, have_cxx, have_flang, run_project


WHOLE_ARRAY_TO_SCALAR_F = """\
      integer function head(n)
      integer n
      head = n
      end

      program p
      integer a(3)
      integer head
      external head
      a(1) = 7
      a(2) = 8
      a(3) = 9
      print *, head(a)
      end
"""


# A rank-1 actual passed to a 2-D explicit-shape dummy whose extents are
# *other dummies* (``V(NR,NC)``) -- the SPICE CORTAB ``VALUES(NCOLS,N)``
# pattern.  The seq_assoc reshape must spell the extents using the actual
# arguments passed for NR/NC at this call site (2 and 3), not the callee's
# dummy names (which don't exist in the caller).
SEQ_ASSOC_DUMMY_BOUNDS_F = """\
      subroutine fill(nr, nc, v)
      integer nr, nc
      double precision v(nr, nc)
      v(1, 1)   = 1.5
      v(nr, nc) = 9.5
      end

      program p
      double precision a(6)
      call fill(2, 3, a)
      print *, a(1), a(6)
      end
"""


# A scalar dummy that is the base of a caller's array, forwarded to an array
# dummy whose callee indexes past element 1 (the SPICE pool-counter idiom:
# scty01 -> zzscup01(scalar POLCTR) -> zzpctrck -> zzctrchk reads CTR(2)).
SCALAR_BASE_OF_ARRAY_F = """\
      subroutine readsecond(ctr, s)
      integer ctr(2), s
      s = ctr(1) + ctr(2)
      end

      subroutine mid(polctr, s)
      integer polctr, s
      call readsecond(polctr, s)
      end

      program p
      integer a(2), s
      a(1) = 10
      a(2) = 20
      call mid(a(1), s)
      print *, s
      end
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class WholeArrayToScalarEmitTests(unittest.TestCase):
    def test_actual_passed_as_first_element(self) -> None:
        cpp = convert_project(WHOLE_ARRAY_TO_SCALAR_F, suffix=".f")
        self.assertIn("head(ftn::first(a))", cpp)

    def test_dummy_bounds_use_caller_actuals(self) -> None:
        cpp = convert_project(SEQ_ASSOC_DUMMY_BOUNDS_F, suffix=".f")
        # Extents NR, NC become the actual arguments 2 and 3 -- not the
        # callee's dummy names.
        self.assertIn("ftn::seq_assoc<2>(a, {1, 1}, {(2), (3)})", cpp)
        self.assertNotIn("{nr, nc}", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class WholeArrayToScalarRunTests(unittest.TestCase):
    def test_scalar_dummy_sees_first_element(self) -> None:
        # head(a) is storage-associated with a(1) == 7.
        self.assertEqual(int(run_project(WHOLE_ARRAY_TO_SCALAR_F, suffix=".f").strip()), 7)

    def test_dummy_bounds_reshape_runs(self) -> None:
        # a(6) views as V(2,3); V(1,1)->a(1)=1.5, V(2,3)->a(6)=9.5
        # (column-major: (2,3) -> offset 1+2*2 = 5 -> a(6)).
        self.assertEqual(
            run_project(SEQ_ASSOC_DUMMY_BOUNDS_F, suffix=".f").split(),
            ["1.5", "9.5"],
        )

    def test_scalar_base_of_array_runs(self) -> None:
        # A scalar dummy (CTR) forwarded to an array dummy (V(2)) whose callee
        # reads V(2): the scalar is the base of the caller's 2-element array,
        # so the read must reach the adjacent element (the SPICE
        # zzscup01/zzpctrck/zzctrchk counter idiom) -- not trip a bounds check.
        self.assertEqual(
            run_project(SCALAR_BASE_OF_ARRAY_F, suffix=".f").split(),
            ["30"],
        )


# A 0-based actual passed to an assumed-size ``ARRAY(*)`` dummy: the dummy
# is 1-based (its *own* declared lb wins), so ``ARRAY(1)`` must reach the
# actual's first element, not be indexed at the actual's lb 0.  The dummy's
# static lower bound makes the rebase happen.
ASSUMED_SIZE_REBASE_F = """\
      subroutine fill(array, n)
      integer n
      double precision array(*)
      integer i
      do i = 1, n
         array(i) = i * 10
      end do
      end

      program p
      double precision q(0:3)
      call fill(q, 4)
      print *, q(0), q(3)
      end
"""


# An explicit non-1 lower bound on an assumed-size dummy (the SPICE LNKINI
# ``POOL(2, LBPOOL:*)`` idiom) must be preserved, so ``POOL(_,0)`` is valid.
EXPLICIT_LOWER_ASSUMED_F = """\
      subroutine setp(pool, k)
      integer k
      integer lbpool
      parameter (lbpool = -5)
      integer pool(2, lbpool:*)
      pool(1, -2) = k
      pool(2,  0) = k + 1
      end

      program p
      integer store(2, -5:10)
      call setp(store, 100)
      print *, store(1,-2), store(2,0)
      end
"""


# A 0-based whole array passed to a 1-based *explicit-shape* dummy
# (``CALL VSCLG(.., Q, 4, OUT)`` where ``Q`` is a quaternion ``Q(0:3)`` and
# the dummy is ``V1(NDIM)``).  A Fortran dummy indexes from its own declared
# lower bound (1 by default), so the dummy must be a *static* 1-based view
# -- otherwise it inherited the actual's lb 0 and the callee's ``V1(4)``
# overran the ``[0,3]`` storage (the SPICE f_quat / f_ck06 / vsclg / vminug
# CRASH).  No call-site rewrite: the fix is purely in the dummy's type.
EXPLICIT_SHAPE_REBASE_F = """\
      subroutine vsclg(s, v1, ndim, vout)
      double precision s, v1(ndim), vout(ndim)
      integer ndim, i
      do i = 1, ndim
         vout(i) = s * v1(i)
      end do
      end

      program p
      double precision q(0:3), r(0:3)
      integer i
      do i = 0, 3
         q(i) = i + 1
      end do
      call vsclg(2.0d0, q, 4, r)
      print *, r(0), r(1), r(2), r(3)
      end
"""


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class ExplicitShapeDummyOneBasedTests(unittest.TestCase):
    def test_dummy_is_static_one_based(self) -> None:
        cpp = convert_project(EXPLICIT_SHAPE_REBASE_F, suffix=".f")
        # The explicit-shape dummy carries a static 1-based lower bound...
        self.assertIn(
            "ftn::ArrayRef<double, 1, std::array<ftn::index_t, 1>{1}> v1", cpp
        )
        # ...and the call site is unchanged (no lb1/seq_assoc wrapper).
        self.assertIn("vsclg(2.0e0, q, 4, r)", cpp)

    def test_runs(self) -> None:
        # q(0:3)=1,2,3,4 scaled by 2 -> r(0:3)=2,4,6,8; no overrun.
        self.assertEqual(
            run_project(EXPLICIT_SHAPE_REBASE_F, suffix=".f").split(),
            ["2", "4", "6", "8"],
        )


# A dummy array whose lower bound is a *named PARAMETER* (the SPICE
# ``LBCELL = -5`` cell lower bound: ``WORK(LBCELL:MW, NW)``).  The named
# constant must be folded to its literal so the dummy gets a static
# ``{-5, ...}`` ``Lower`` -- the name itself can't appear in the signature's
# type, so it fell back to the runtime sentinel, lost the -5, and the cell's
# control element ``WORK(LBCELL,I)`` overran the [1,..] storage (the SPICE
# f_zzgflng / f_zzgfcslv elem_tail index-(-5) CRASH).  A CHARACTER cell is
# left in the runtime form (no static-lower CharArrayRef conversion).
NAMED_LBCELL_F = """\
      subroutine ssz(n, cell)
      integer n, lbcell
      parameter (lbcell=-5)
      double precision cell(lbcell:n)
      cell(lbcell) = n
      cell(1) = 1.0
      end

      subroutine usecell(work, mw, nw)
      integer mw, nw, i, lbcell
      parameter (lbcell=-5)
      double precision work(lbcell:mw, nw)
      do i = 1, nw
         call ssz(mw, work(lbcell, i))
      end do
      end

      program p
      integer lbcell, i, j
      parameter (lbcell=-5)
      double precision w(lbcell:10, 3)
      do j = 1, 3
         do i = lbcell, 10
            w(i,j) = 0
         end do
      end do
      call usecell(w, 10, 3)
      print *, w(lbcell,1), w(lbcell,3)
      end
"""


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class NamedConstantLowerBoundTests(unittest.TestCase):
    def test_named_param_lower_folds_to_static(self) -> None:
        cpp = convert_project(NAMED_LBCELL_F, suffix=".f")
        # LBCELL (=-5) folded into the dummy's static Lower NTTP.
        self.assertIn(
            "ftn::ArrayRef<double, 2, std::array<ftn::index_t, 2>{-5,1}> work",
            cpp,
        )

    def test_runs(self) -> None:
        # ssz writes work(LBCELL,i)=MW=10 for each column; w(-5,1)=w(-5,3)=10.
        self.assertEqual(
            run_project(NAMED_LBCELL_F, suffix=".f").split(),
            ["10", "10"],
        )


# An assumed-size ``(*)`` dummy forwarded down a chain of ``(*)`` dummies:
# Fortran puts no upper-bound check on the last dimension, so the callee may
# index as far as the actual's real storage.  Each ``(*)`` dummy is
# normalized at entry to an unbounded last extent so a legitimate index past
# the received view's tracked extent does not trip a debug bounds check (the
# SPICE DAS ``MOVED(DATAD, N, ...)`` / f_ek02 collapse).
ASSUMED_SIZE_CHAIN_F = """\
      subroutine inner(x)
      double precision x(*)
      x(3) = 33.0d0
      end

      subroutine outer(x)
      double precision x(*)
      call inner(x)
      end

      program p
      double precision buf(3)
      buf(1) = 1.0d0
      buf(2) = 2.0d0
      call outer(buf)
      print *, buf(1), buf(2), buf(3)
      end
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class AssumedSizeChainNormalizeTests(unittest.TestCase):
    def test_dummy_normalized_at_entry(self) -> None:
        cpp = convert_project(ASSUMED_SIZE_CHAIN_F, suffix=".f")
        # Both (*) dummies reset their view to unbounded at entry.
        self.assertIn("x = ftn::assume_size(x);", cpp)

    @unittest.skipUnless(have_cxx(), "need a C++20 compiler")
    def test_runs(self) -> None:
        self.assertEqual(
            run_project(ASSUMED_SIZE_CHAIN_F, suffix=".f").split(),
            ["1", "2", "33"],
        )


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")  # noqa: E501
class AssumedSizeLowerBoundTests(unittest.TestCase):
    def test_assumed_size_dummy_is_one_based(self) -> None:
        cpp = convert_project(ASSUMED_SIZE_REBASE_F, suffix=".f")
        # The dummy carries a static lower bound of 1.
        self.assertIn(
            "ftn::ArrayRef<double, 1, std::array<ftn::index_t, 1>{1}> array",
            cpp,
        )

    def test_assumed_size_rebase_runs(self) -> None:
        # array(1..4)=10..40 maps onto q(0..3); q(0)=10, q(3)=40.
        self.assertEqual(
            run_project(ASSUMED_SIZE_REBASE_F, suffix=".f").split(),
            ["10", "40"],
        )

    def test_explicit_lower_assumed_size_preserved(self) -> None:
        cpp = convert_project(EXPLICIT_LOWER_ASSUMED_F, suffix=".f")
        self.assertIn(
            "std::array<ftn::index_t, 2>{1,-5}", cpp
        )

    def test_explicit_lower_assumed_size_runs(self) -> None:
        self.assertEqual(
            run_project(EXPLICIT_LOWER_ASSUMED_F, suffix=".f").split(),
            ["100", "101"],
        )


# Passing an array *element* to an assumed-size dummy (``CALL MOVEIT(A, 4,
# B(3))`` -> the SPICE MOVED/MOVEI/VSCLG idiom): the dummy views ``B`` from
# that element to the end.  The dummy's extent is unknown (``DST(*)``), so
# the view must use the remaining storage -- using the assumed-size
# placeholder made an empty view and any callee index overran it.
ELEM_TO_ASSUMED_F = """\
      subroutine moveit(src, n, dst)
      integer n, src(*), dst(*), i
      do i = 1, n
         dst(i) = src(i)
      end do
      end

      program p
      integer a(10), b(10), i
      do i = 1, 10
         a(i) = i
         b(i) = 0
      end do
      call moveit(a, 4, b(3))
      print *, b(3), b(6), b(7)
      end
"""


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class ElementToAssumedSizeTests(unittest.TestCase):
    def test_uses_elem_tail_not_empty_view(self) -> None:
        cpp = convert_project(ELEM_TO_ASSUMED_F, suffix=".f")
        # Remaining-storage view, not elem_tail_n with an empty extent.
        self.assertIn("ftn::elem_tail(b, 3)", cpp)
        self.assertNotIn("elem_tail_n(b", cpp)

    def test_runs(self) -> None:
        # a(1..4)=1,2,3,4 -> b(3..6); b(7) stays 0.
        self.assertEqual(
            run_project(ELEM_TO_ASSUMED_F, suffix=".f").split(),
            ["1", "4", "0"],
        )


# The SPICE EK write-path collapse (f_ek02): a *scalar* dummy that is
# storage-associated with a caller's array (``zzekue04``'s ``INTEGER IVALS``)
# is forwarded to an assumed-size ``(*)`` dummy (``zzekad04``'s ``IVALS(*)``),
# which passes one of its *elements* to a deeper ``(*)`` dummy (``dasudi`` ->
# ``dasuri``) that writes two words.  The scalar->array view is assumed-size
# (sentinel extent, size()==1); ``elem_tail`` on it must stay assumed-size,
# not compute ``size()-offset`` (which collapses to 1 and makes the deep
# ``d(2)`` write trip the bounds check).
SCALAR_TO_ASSUMED_ELEM_F = """\
      subroutine writetwo(d)
      integer d(*)
      d(1) = 111
      d(2) = 222
      end

      subroutine mid(vals)
      integer vals(*)
      call writetwo(vals(1))
      end

      subroutine top(v)
      integer v
      call mid(v)
      end

      program p
      integer a(2)
      a(1) = 0
      a(2) = 0
      call top(a(1))
      print *, a(1), a(2)
      end
"""


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class ScalarToAssumedSizeElementTests(unittest.TestCase):
    def test_deep_write_reaches_second_element(self) -> None:
        # top(a(1)) -> scalar v -> mid's vals(*) (assumed-size) -> element
        # vals(1) -> writetwo's d(*); d(2) must reach a(2).
        self.assertEqual(
            run_project(SCALAR_TO_ASSUMED_ELEM_F, suffix=".f").split(),
            ["111", "222"],
        )


# An array *element* whose array shadows a same-named global subprogram.
# SPICE has an ``INTEGER FUNCTION POS`` *and* routines (SPKGPS) with a dummy
# array ``POS(3)`` passed element-wise: ``CALL MXV(ROT, POS(1), STEMP)``.
# The sequence-association reshape must recognise ``pos(1)`` as an array
# element (the local array shadows the global function) and wrap it in
# ``elem_tail_n``; otherwise the bare element decayed to a single-cell
# ``{1}`` view via ``ArrayRef(T&)`` and the callee's ``VIN(2)`` overran it.
ELEM_SHADOWS_FUNCTION_F = """\
      integer function pos(str, sub)
      character*(*) str, sub
      pos = index(str, sub)
      end

      subroutine mxv3(vin, vout)
      double precision vin(3), vout(3)
      integer i
      do i = 1, 3
         vout(i) = vin(i) * 2
      end do
      end

      subroutine outer(pos, res)
      double precision pos(3), res(3)
      call mxv3(pos(1), res)
      end

      program p
      double precision a(3), r(3)
      a(1) = 1
      a(2) = 2
      a(3) = 3
      call outer(a, r)
      print *, r(1), r(2), r(3)
      end
"""


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class ElementShadowingFunctionTests(unittest.TestCase):
    def test_local_array_shadows_global_function(self) -> None:
        cpp = convert_project(ELEM_SHADOWS_FUNCTION_F, suffix=".f")
        # pos(1) recognised as an array element despite the global pos()
        # function -> reshaped to a 3-element view, not a bare element.
        self.assertIn("mxv3(ftn::elem_tail_n(pos, 3, 1), res)", cpp)

    def test_runs(self) -> None:
        self.assertEqual(
            run_project(ELEM_SHADOWS_FUNCTION_F, suffix=".f").split(),
            ["2", "4", "6"],
        )


# A multi-dimensional array *element* passed to a higher-rank explicit-shape
# dummy: ``CALL MXM(REF(1,1,K), ...)`` where REF is (3,3,5) and the dummy is
# M(3,3) -- the K-th 3x3 slice.  Without this the element became a (1,1) view
# and the callee's M(2,_) overran it (the SPICE MXM/MTXM matrix idiom).
MULTID_ELEM_F = """\
      subroutine cp(a, b)
      double precision a(3,3), b(3,3)
      integer i, j
      do i = 1, 3
         do j = 1, 3
            b(i,j) = a(i,j)
         end do
      end do
      end

      program p
      double precision ref(3,3,5), t(3,3)
      integer i, j, k
      do k = 1, 5
         do i = 1, 3
            do j = 1, 3
               ref(i,j,k) = i + 3*(j-1) + 9*(k-1)
            end do
         end do
      end do
      call cp(ref(1,1,3), t)
      print *, t(1,1), t(3,3)
      end
"""


# A rank-1 actual passed to a 2-D *assumed-size* dummy whose leading dim is
# another dummy (``A(M, *)`` -- the SPICE ``SPKW01``/``DLINES(DLSIZE,*)``
# idiom).  The leading extent ``M`` must be kept (collapsing it to the
# assumed placeholder lost the real row count), and the trailing ``*`` dim
# must span the rest of the actual's storage (``actual.size() / M``), not be
# left as the placeholder 0 (which made an empty column and any callee write
# to column 2+ overran the view).
RANK1_TO_ASSUMED_SIZE_2D_F = """\
      subroutine fill(m, a)
      integer m
      double precision a(m, *)
      a(1,1) = 1.0
      a(2,1) = 2.0
      a(1,2) = 3.0
      a(2,2) = 4.0
      end

      program p
      double precision q(8)
      integer i
      do i = 1, 8
         q(i) = 0
      end do
      call fill(2, q)
      print *, q(1), q(2), q(3), q(4)
      end
"""


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class Rank1ToAssumedSize2DTests(unittest.TestCase):
    def test_leading_extent_kept_trailing_from_size(self) -> None:
        cpp = convert_project(RANK1_TO_ASSUMED_SIZE_2D_F, suffix=".f")
        # Leading dim M -> (2); trailing assumed dim -> actual.size() / M.
        self.assertIn(
            "ftn::seq_assoc<2>(q, {1, 1}, {(2), (q.size()) / (((2)))})", cpp
        )

    def test_runs(self) -> None:
        # column-major (2,*) view of q: a(1,1)=q(1), a(2,1)=q(2),
        # a(1,2)=q(3), a(2,2)=q(4).
        self.assertEqual(
            run_project(RANK1_TO_ASSUMED_SIZE_2D_F, suffix=".f").split(),
            ["1", "2", "3", "4"],
        )


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class MultiDimElementToDummyTests(unittest.TestCase):
    def test_uses_seq_assoc_at(self) -> None:
        cpp = convert_project(MULTID_ELEM_F, suffix=".f")
        self.assertIn("ftn::seq_assoc_at<2>(ref, {1, 1}, {3, 3}, 1, 1, 3)", cpp)

    def test_runs(self) -> None:
        # slice k=3: ref(i,j,3)=i+3(j-1)+18; t(1,1)=19, t(3,3)=27.
        self.assertEqual(
            run_project(MULTID_ELEM_F, suffix=".f").split(),
            ["19", "27"],
        )


# A multi-dimensional array *element* passed to a higher-rank *assumed-size*
# dummy (``PLATES(3, *)`` -- the SPICE ``ZZELLPLT``/``ZZCAPPLT(.., PLATES(1,
# PIX))`` idiom).  The dummy's leading extent (3) is real but its trailing
# ``*`` is caller-sized, so the view must span the rest of the actual's
# storage from the element.  Without this the element decayed through the
# ``ArrayRef(T&)`` scalar constructor to a single-cell ``{1,1}`` view and the
# callee's ``PLATES(2,_)`` overran it ("index 2 out of range [1, 1]") --
# the dominant tspice CRASH signature.
MULTID_ELEM_ASSUMED_F = """\
      subroutine cap(plates, n)
      integer plates(3, *), n, i
      do i = 1, n
         plates(1, i) = 10 + i
         plates(2, i) = 20 + i
         plates(3, i) = 30 + i
      end do
      end

      program p
      integer plt(3, 10), pix, i, j
      do i = 1, 3
         do j = 1, 10
            plt(i, j) = 0
         end do
      end do
      pix = 4
      call cap(plt(1, pix), 2)
      print *, plt(2,4), plt(3,5), plt(1,6)
      end
"""


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class MultiDimElementToAssumedSizeTests(unittest.TestCase):
    def test_uses_seq_assoc_at_rest(self) -> None:
        cpp = convert_project(MULTID_ELEM_ASSUMED_F, suffix=".f")
        # Leading extent 3 kept; trailing assumed dim -> 0 placeholder that
        # the runtime fills from the remaining storage.
        self.assertIn(
            "ftn::seq_assoc_at_rest<2>(plt, {1, 1}, {3, 0}, 1, pix)", cpp
        )

    def test_runs(self) -> None:
        # cap writes plates(1..3, 1..2) starting at plt(1,4):
        # plt(2,4)=21, plt(3,5)=32; plt(1,6) untouched = 0.
        self.assertEqual(
            run_project(MULTID_ELEM_ASSUMED_F, suffix=".f").split(),
            ["21", "32", "0"],
        )


if __name__ == "__main__":
    unittest.main()
