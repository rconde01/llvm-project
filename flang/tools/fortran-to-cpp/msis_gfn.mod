!mod$ v1 sum:5827e8e3f718e348
!need$ 9f3b445d75a042d3 n msis_init
!need$ edee782e91b5b56c n msis_constants
module msis_gfn
use msis_constants,only:rp
use msis_constants,only:maxn
use msis_init,only:tn
use msis_init,only:pr
use msis_init,only:n2
use msis_init,only:o2
use msis_init,only:o1
use msis_init,only:he
use msis_init,only:h1
use msis_init,only:ar
use msis_init,only:n1
use msis_init,only:oa
use msis_init,only:no
use msis_init,only:swg
real(4)::plg(0_8:6_8,0_8:6_8)
real(4)::cdoy(1_8:2_8)
real(4)::sdoy(1_8:2_8)
real(4)::clst(1_8:3_8)
real(4)::slst(1_8:3_8)
real(4)::clon(1_8:2_8)
real(4)::slon(1_8:2_8)
real(4)::sfluxavgref
real(4)::sfluxavg_quad_cutoff
real(4)::lastlat
real(4)::lastdoy
real(4)::lastlst
real(4)::lastlon
contains
subroutine globe(doy,utsec,lat,lon,sfluxavg,sflux,ap,bf)
real(4),intent(in)::doy
real(4),intent(in)::utsec
real(4),intent(in)::lat
real(4),intent(in)::lon
real(4),intent(in)::sfluxavg
real(4),intent(in)::sflux
real(4),intent(in)::ap(1_8:7_8)
real(4),intent(out)::bf(0_8:511_8)
end
function solzen(ddd,lst,lat,lon)
real(4),intent(in)::ddd
real(4),intent(in)::lst
real(4),intent(in)::lat
real(4),intent(in)::lon
real(4)::solzen
end
function sfluxmod(iz,gf,parmset,dffact)
use msis_init,only:basissubset
integer(4),intent(in)::iz
real(4),intent(in)::gf(0_8:511_8)
type(basissubset),intent(in)::parmset
real(4),intent(in)::dffact
real(4)::sfluxmod
end
function geomag(p0,bf,plg)
real(4),intent(in)::p0(0_8:53_8)
real(4),intent(in)::bf(0_8:12_8)
real(4),intent(in)::plg(0_8:6_8,0_8:1_8)
real(4)::geomag
end
function utdep(p0,bf)
real(4),intent(in)::p0(0_8:11_8)
real(4),intent(in)::bf(0_8:8_8)
real(4)::utdep
end
end
