# -*- coding: utf-8 -*-

"""
piecewise_rational.rationalcubic
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Shape-preserving piecewise rational cubic interpolation.

Original implementation from Peter Jaeckel's LetsBeRational.

:copyright: 2017 Gammon Capital LLC
:license: MIT, see LICENSE for more details.

References:
- R. Delbourgo, J.A. Gregory, "Shape preserving piecewise rational interpolation",
  SIAM Journal on Scientific and Statistical Computing, 1985.
  http://dspace.brunel.ac.uk/bitstream/2438/2200/1/TR_10_83.pdf
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

from math import fabs, sqrt

from piecewise_rational.constants import DBL_EPSILON, DBL_MIN, DBL_MAX


minimum_rational_cubic_control_parameter_value = -(1 - sqrt(DBL_EPSILON))
maximum_rational_cubic_control_parameter_value = 2 / (DBL_EPSILON * DBL_EPSILON)


def _is_zero(x):
    return fabs(x) < DBL_MIN


def rational_cubic_control_parameter_to_fit_second_derivative_at_left_side(
        x_l, x_r, y_l, y_r, d_l, d_r, second_derivative_l):
    """
    Compute the rational cubic control parameter to fit a second derivative at the left side.

    Parameters
    ----------
    x_l : float
        Left endpoint x-coordinate.
    x_r : float
        Right endpoint x-coordinate.
    y_l : float
        Left endpoint y-value.
    y_r : float
        Right endpoint y-value.
    d_l : float
        Derivative at left endpoint.
    d_r : float
        Derivative at right endpoint.
    second_derivative_l : float
        Target second derivative at left endpoint.

    Returns
    -------
    float
        The control parameter r.
    """
    h = (x_r - x_l)
    numerator = 0.5 * h * second_derivative_l + (d_r - d_l)
    if _is_zero(numerator):
        return 0
    denominator = (y_r - y_l) / h - d_l
    if _is_zero(denominator):
        return maximum_rational_cubic_control_parameter_value if numerator > 0 else minimum_rational_cubic_control_parameter_value
    return numerator / denominator


def minimum_rational_cubic_control_parameter(d_l, d_r, s, preferShapePreservationOverSmoothness):
    """
    Compute the minimum control parameter for shape-preserving interpolation.

    Parameters
    ----------
    d_l : float
        Derivative at left endpoint.
    d_r : float
        Derivative at right endpoint.
    s : float
        Slope of the secant line (y_r - y_l) / (x_r - x_l).
    preferShapePreservationOverSmoothness : bool
        If True, prefer shape preservation over smoothness when there's a conflict.

    Returns
    -------
    float
        The minimum control parameter that maintains shape preservation.
    """
    monotonic = d_l * s >= 0 and d_r * s >= 0
    convex = d_l <= s <= d_r
    concave = d_l >= s >= d_r
    if not monotonic and not convex and not concave:
        return minimum_rational_cubic_control_parameter_value
    d_r_m_d_l = d_r - d_l
    d_r_m_s = d_r - s
    s_m_d_l = s - d_l
    r1 = -DBL_MAX
    r2 = r1
    # If monotonicity on this interval is possible, set r1 to satisfy the monotonicity condition (3.8).
    if monotonic:
        if not _is_zero(s):  # (3.8), avoiding division by zero.
            r1 = (d_r + d_l) / s  # (3.8)
        elif preferShapePreservationOverSmoothness:
            r1 = maximum_rational_cubic_control_parameter_value

    if convex or concave:
        if not (_is_zero(s_m_d_l) or _is_zero(d_r_m_s)):  # (3.18), avoiding division by zero.
            r2 = max(fabs(d_r_m_d_l / d_r_m_s), fabs(d_r_m_d_l / s_m_d_l))
        elif preferShapePreservationOverSmoothness:
            r2 = maximum_rational_cubic_control_parameter_value
    elif monotonic and preferShapePreservationOverSmoothness:
        r2 = maximum_rational_cubic_control_parameter_value
    return max(minimum_rational_cubic_control_parameter_value, max(r1, r2))


def rational_cubic_control_parameter_to_fit_second_derivative_at_right_side(
        x_l, x_r, y_l, y_r, d_l, d_r, second_derivative_r):
    """
    Compute the rational cubic control parameter to fit a second derivative at the right side.

    Parameters
    ----------
    x_l : float
        Left endpoint x-coordinate.
    x_r : float
        Right endpoint x-coordinate.
    y_l : float
        Left endpoint y-value.
    y_r : float
        Right endpoint y-value.
    d_l : float
        Derivative at left endpoint.
    d_r : float
        Derivative at right endpoint.
    second_derivative_r : float
        Target second derivative at right endpoint.

    Returns
    -------
    float
        The control parameter r.
    """
    h = (x_r - x_l)
    numerator = 0.5 * h * second_derivative_r + (d_r - d_l)
    if _is_zero(numerator):
        return 0
    denominator = d_r - (y_r - y_l) / h
    if _is_zero(denominator):
        return maximum_rational_cubic_control_parameter_value if numerator > 0 else minimum_rational_cubic_control_parameter_value
    return numerator / denominator


def convex_rational_cubic_control_parameter_to_fit_second_derivative_at_right_side(
        x_l, x_r, y_l, y_r, d_l, d_r, second_derivative_r,
        preferShapePreservationOverSmoothness):
    """
    Compute the convex rational cubic control parameter at the right side.

    This version ensures the control parameter is at least as large as the
    minimum required for shape preservation.

    Parameters
    ----------
    x_l : float
        Left endpoint x-coordinate.
    x_r : float
        Right endpoint x-coordinate.
    y_l : float
        Left endpoint y-value.
    y_r : float
        Right endpoint y-value.
    d_l : float
        Derivative at left endpoint.
    d_r : float
        Derivative at right endpoint.
    second_derivative_r : float
        Target second derivative at right endpoint.
    preferShapePreservationOverSmoothness : bool
        If True, prefer shape preservation over smoothness.

    Returns
    -------
    float
        The control parameter r.
    """
    r = rational_cubic_control_parameter_to_fit_second_derivative_at_right_side(
        x_l, x_r, y_l, y_r, d_l, d_r, second_derivative_r)
    r_min = minimum_rational_cubic_control_parameter(
        d_l, d_r, (y_r - y_l) / (x_r - x_l), preferShapePreservationOverSmoothness)
    return max(r, r_min)


def rational_cubic_interpolation(x, x_l, x_r, y_l, y_r, d_l, d_r, r):
    """
    Perform rational cubic interpolation.

    Parameters
    ----------
    x : float
        The point at which to evaluate the interpolant.
    x_l : float
        Left endpoint x-coordinate.
    x_r : float
        Right endpoint x-coordinate.
    y_l : float
        Left endpoint y-value.
    y_r : float
        Right endpoint y-value.
    d_l : float
        Derivative at left endpoint.
    d_r : float
        Derivative at right endpoint.
    r : float
        Control parameter. Should be > -1. Large values give linear interpolation.

    Returns
    -------
    float
        The interpolated value at x.
    """
    h = (x_r - x_l)
    if fabs(h) <= 0:
        return 0.5 * (y_l + y_r)
    # r should be greater than -1. We do not use assert(r > -1) here in order to allow values such as NaN to be propagated.
    t = (x - x_l) / h
    if not (r >= maximum_rational_cubic_control_parameter_value):
        t = (x - x_l) / h
        omt = 1 - t
        t2 = t * t
        omt2 = omt * omt
        # Formula (2.4) divided by formula (2.5)
        return (y_r * t2 * t + (r * y_r - h * d_r) * t2 * omt + (r * y_l + h * d_l) * t * omt2 + y_l * omt2 * omt) / (1 + (r - 3) * t * omt)

    # Linear interpolation without over-or underflow.
    return y_r * t + y_l * (1 - t)


def convex_rational_cubic_control_parameter_to_fit_second_derivative_at_left_side(
        x_l, x_r, y_l, y_r, d_l, d_r, second_derivative_l, preferShapePreservationOverSmoothness):
    """
    Compute the convex rational cubic control parameter at the left side.

    This version ensures the control parameter is at least as large as the
    minimum required for shape preservation.

    Parameters
    ----------
    x_l : float
        Left endpoint x-coordinate.
    x_r : float
        Right endpoint x-coordinate.
    y_l : float
        Left endpoint y-value.
    y_r : float
        Right endpoint y-value.
    d_l : float
        Derivative at left endpoint.
    d_r : float
        Derivative at right endpoint.
    second_derivative_l : float
        Target second derivative at left endpoint.
    preferShapePreservationOverSmoothness : bool
        If True, prefer shape preservation over smoothness.

    Returns
    -------
    float
        The control parameter r.
    """
    r = rational_cubic_control_parameter_to_fit_second_derivative_at_left_side(
        x_l, x_r, y_l, y_r, d_l, d_r, second_derivative_l)
    r_min = minimum_rational_cubic_control_parameter(
        d_l, d_r, (y_r - y_l) / (x_r - x_l), preferShapePreservationOverSmoothness)
    return max(r, r_min)
