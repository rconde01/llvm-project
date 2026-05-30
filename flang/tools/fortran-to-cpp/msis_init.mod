!mod$ v1 sum:861f1e1319827035
!need$ edee782e91b5b56c n msis_constants
module msis_init
use msis_constants,only:rp
use msis_constants,only:nspec
use msis_constants,only:nl
use msis_constants,only:maxnbf
use msis_constants,only:mbf
logical(4)::initflag
logical(4)::haveparmspace
logical(4)::zaltflag
logical(4)::specflag(1_8:10_8)
logical(4)::massflag(1_8:10_8)
logical(4)::n2rflag
logical(4)::zsfx(0_8:383_8)
logical(4)::tsfx(0_8:383_8)
logical(4)::psfx(0_8:383_8)
logical(4)::smod(0_8:23_8)
logical(4)::swg(0_8:511_8)
real(4)::masswgt(1_8:10_8)
real(4)::swleg(1_8:25_8)
real(4)::swc(1_8:25_8)
real(4)::sav(1_8:25_8)
type::basissubset
sequence
character(8_4,1)::name
integer(4)::bl
integer(4)::nl
real(4),allocatable::beta(:,:)
logical(4),allocatable::active(:,:)
integer(4),allocatable::fitb(:,:)
end type
type(basissubset)::tn
type(basissubset)::pr
type(basissubset)::n2
type(basissubset)::o2
type(basissubset)::o1
type(basissubset)::he
type(basissubset)::h1
type(basissubset)::ar
type(basissubset)::n1
type(basissubset)::oa
type(basissubset)::no
integer(4)::nvertparm
real(4)::etatn(0_8:30_8,2_8:6_8)
real(4)::etao1(0_8:30_8,2_8:6_8)
real(4)::etano(0_8:30_8,2_8:6_8)
real(4)::hrfacto1ref
real(4)::dhrfacto1ref
real(4)::hrfactnoref
real(4)::dhrfactnoref
contains
subroutine msisinit(parmpath,parmfile,iun,switch_gfn,switch_legacy,lzalt_type,lspec_select,lmass_include,ln2_msis00)
character(*,1),intent(in),optional::parmpath
character(*,1),intent(in),optional::parmfile
integer(4),intent(in),optional::iun
logical(4),intent(in),optional::switch_gfn(0_8:511_8)
real(4),intent(in),optional::switch_legacy(1_8:25_8)
logical(4),intent(in),optional::lzalt_type
logical(4),intent(in),optional::lspec_select(1_8:10_8)
logical(4),intent(in),optional::lmass_include(1_8:10_8)
logical(4),intent(in),optional::ln2_msis00
end
subroutine initparmspace()
end
subroutine loadparmset(name,iun)
character(*,1),intent(in)::name
integer(4),intent(in)::iun
end
subroutine pressparm()
end
subroutine tselec(sv)
real(4),intent(in)::sv(1_8:25_8)
end
subroutine tretrv(svv)
real(4),intent(out)::svv(1_8:25_8)
end
end
