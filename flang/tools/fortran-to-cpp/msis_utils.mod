!mod$ v1 sum:dcdbb36f0754dc8d
!need$ edee782e91b5b56c n msis_constants
module msis_utils
contains
function alt2gph(lat,alt)
real(8),intent(in)::lat
real(8),intent(in)::alt
real(8)::alt2gph
end
function gph2alt(theta,gph)
real(8),intent(in)::theta
real(8),intent(in)::gph
real(8)::gph2alt
end
subroutine bspline(x,nodes,nd,kmax,eta,s,i)
real(4),intent(in)::x
real(4),intent(in)::nodes(0_8:)
integer(4),intent(in)::nd
integer(4),intent(in)::kmax
real(4),intent(in)::eta(0_8:30_8,2_8:6_8)
real(4),intent(out)::s(-5_8:0_8,2_8:6_8)
integer(4),intent(out)::i
end
function dilog(x0)
real(4),intent(in)::x0
real(4)::dilog
end
end
