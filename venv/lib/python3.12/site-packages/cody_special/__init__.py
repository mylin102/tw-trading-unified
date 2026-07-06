"""
cody-special: High-precision error functions and normal distribution.

Based on W.J. Cody's rational Chebyshev approximations (1969) as implemented
by Peter Jäckel in LetsBeRational.

References:
- W.J. Cody, "Rational Chebyshev approximations for the error function",
  Math. Comp., 1969, pp. 631-638.
- Peter Jäckel, "Let's Be Rational", 2013-2014, www.jaeckel.org
"""

from cody_special.erf_cody import erf_cody, erfc_cody, erfcx_cody
from cody_special.normaldistribution import norm_pdf, norm_cdf, inverse_norm_cdf

__version__ = "1.0.0"
__all__ = [
    "erf_cody",
    "erfc_cody",
    "erfcx_cody",
    "norm_pdf",
    "norm_cdf",
    "inverse_norm_cdf",
]
