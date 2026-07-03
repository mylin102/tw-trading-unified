# -*- coding: utf-8 -*-

"""
cody_special.erf_cody
~~~~~~~~~~~~~~~~~~~~~

High-precision error function implementations based on W.J. Cody's
rational Chebyshev approximations.

Original implementation from Peter Jaeckel's LetsBeRational.

:copyright: 2017 Gammon Capital LLC
:license: MIT, see LICENSE for more details.

References:
- W.J. Cody, "Rational Chebyshev approximations for the error function",
  Math. Comp., 1969, pp. 631-638.
- Peter Jaeckel, "Let's Be Rational", 2013-2014, www.jaeckel.org

======================================================================================
Copyright 2013-2014 Peter Jaeckel.

Permission to use, copy, modify, and distribute this software is freely granted,
provided that this notice is preserved.

WARRANTY DISCLAIMER
The Software is provided "as is" without warranty of any kind, either express or implied,
including without limitation any implied warranties of condition, uninterrupted use,
merchantability, fitness for a particular purpose, or non-infringement.
======================================================================================
"""

from math import floor, fabs, exp


def d_int(x):
    return floor(x) if x > 0 else -floor(-x)


A = (3.1611237438705656, 113.864154151050156, 377.485237685302021, 3209.37758913846947, .185777706184603153)
B = (23.6012909523441209, 244.024637934444173, 1282.61652607737228, 2844.23683343917062)
C = (.564188496988670089, 8.88314979438837594, 66.1191906371416295, 298.635138197400131, 881.95222124176909,
     1712.04761263407058, 2051.07837782607147, 1230.33935479799725, 2.15311535474403846e-8)
D = (15.7449261107098347, 117.693950891312499, 537.181101862009858, 1621.38957456669019, 3290.79923573345963,
     4362.61909014324716, 3439.36767414372164, 1230.33935480374942)
P = (.305326634961232344, .360344899949804439, .125781726111229246, .0160837851487422766, 6.58749161529837803e-4,
     .0163153871373020978)
Q = (2.56852019228982242, 1.87295284992346047, .527905102951428412, .0605183413124413191, .00233520497626869185)

ZERO = 0.
HALF = .5
ONE = 1.
TWO = 2.
FOUR = 4.
SQRPI = 0.56418958354775628695
THRESH = .46875
SIXTEEN = 16.

# Machine-dependent constants for IEEE double precision
XINF = 1.79e308
XNEG = -26.628
XSMALL = 1.11e-16
XBIG = 26.543
XHUGE = 6.71e7
XMAX = 2.53e307


def calerf(x, jint):
    """
    Evaluate erf(x), erfc(x), or exp(x*x)*erfc(x) for a real argument x.

    This routine evaluates near-minimax approximations from "Rational Chebyshev
    approximations for the error function" by W.J. Cody, Math. Comp., 1969,
    pp. 631-638.

    Parameters
    ----------
    x : float
        The argument to evaluate.
    jint : int
        Selects the function:
        - 0: erf(x)
        - 1: erfc(x)
        - 2: erfcx(x) = exp(x*x) * erfc(x)

    Returns
    -------
    float
        The computed function value.

    Author: W.J. Cody
            Mathematics and Computer Science Division
            Argonne National Laboratory
            Argonne, IL 60439
    Latest modification: March 19, 1990
    """
    y = fabs(x)

    if y <= THRESH:
        # Evaluate erf for |X| <= 0.46875
        ysq = ZERO
        if y > XSMALL:
            ysq = y * y

        xnum = A[4] * ysq
        xden = ysq
        for i__ in range(0, 3):
            xnum = (xnum + A[i__]) * ysq
            xden = (xden + B[i__]) * ysq

        result = x * (xnum + A[3]) / (xden + B[3])
        if jint != 0:
            result = ONE - result

        if jint == 2:
            result *= exp(ysq)

        return result

    elif y <= FOUR:
        # Evaluate erfc for 0.46875 <= |X| <= 4.0
        xnum = C[8] * y
        xden = y
        for i__ in range(0, 7):
            xnum = (xnum + C[i__]) * y
            xden = (xden + D[i__]) * y

        result = (xnum + C[7]) / (xden + D[7])
        if jint != 2:
            d__1 = y * SIXTEEN
            ysq = d_int(d__1) / SIXTEEN
            _del = (y - ysq) * (y + ysq)
            d__1 = exp(-ysq * ysq) * exp(-_del)
            result *= d__1

    else:
        # Evaluate erfc for |X| > 4.0
        result = ZERO
        if y >= XBIG:
            if jint != 2 or y >= XMAX:
                return fix_up_for_negative_argument_erf_etc(jint, result, x)
            if y >= XHUGE:
                result = SQRPI / y
                return fix_up_for_negative_argument_erf_etc(jint, result, x)

        ysq = ONE / (y * y)
        xnum = P[5] * ysq
        xden = ysq
        for i__ in range(0, 4):
            xnum = (xnum + P[i__]) * ysq
            xden = (xden + Q[i__]) * ysq

        result = ysq * (xnum + P[4]) / (xden + Q[4])
        result = (SQRPI - result) / y
        if jint != 2:
            d__1 = y * SIXTEEN
            ysq = d_int(d__1) / SIXTEEN
            _del = (y - ysq) * (y + ysq)
            d__1 = exp(-ysq * ysq) * exp(-_del)
            result *= d__1

    return fix_up_for_negative_argument_erf_etc(jint, result, x)


def fix_up_for_negative_argument_erf_etc(jint, result, x):
    """Fix up for negative argument, erf, etc."""
    if jint == 0:
        result = (HALF - result) + HALF
        if x < ZERO:
            result = -result

    elif jint == 1:
        if x < ZERO:
            result = TWO - result
    else:
        if x < ZERO:
            if x < XNEG:
                result = XINF
            else:
                d__1 = x * SIXTEEN
                ysq = d_int(d__1) / SIXTEEN
                _del = (x - ysq) * (x + ysq)
                y = exp(ysq * ysq) * exp(_del)
                result = y + y - result
    return result


def erf_cody(x):
    """
    Compute the error function erf(x).

    Parameters
    ----------
    x : float
        The argument.

    Returns
    -------
    float
        erf(x), the error function evaluated at x.

    Author: W.J. Cody, January 8, 1985
    """
    return calerf(x, 0)


def erfc_cody(x):
    """
    Compute the complementary error function erfc(x) = 1 - erf(x).

    Parameters
    ----------
    x : float
        The argument.

    Returns
    -------
    float
        erfc(x), the complementary error function evaluated at x.

    Author: W.J. Cody, January 8, 1985
    """
    return calerf(x, 1)


def erfcx_cody(x):
    """
    Compute the scaled complementary error function erfcx(x) = exp(x*x) * erfc(x).

    Parameters
    ----------
    x : float
        The argument.

    Returns
    -------
    float
        erfcx(x), the scaled complementary error function evaluated at x.

    Author: W.J. Cody, March 30, 1987
    """
    return calerf(x, 2)
