"""
piecewise-rational: Shape-preserving piecewise rational cubic interpolation.

Implementation of the Delbourgo-Gregory algorithm for monotonic and convex
interpolation, as used in Peter Jäckel's LetsBeRational.

References:
- R. Delbourgo, J.A. Gregory, "Shape preserving piecewise rational interpolation",
  SIAM Journal on Scientific and Statistical Computing, 1985.
- Peter Jäckel, "Let's Be Rational", 2013-2014, www.jaeckel.org
"""

from piecewise_rational.rationalcubic import (
    rational_cubic_interpolation,
    rational_cubic_control_parameter_to_fit_second_derivative_at_left_side,
    rational_cubic_control_parameter_to_fit_second_derivative_at_right_side,
    convex_rational_cubic_control_parameter_to_fit_second_derivative_at_left_side,
    convex_rational_cubic_control_parameter_to_fit_second_derivative_at_right_side,
    minimum_rational_cubic_control_parameter,
)

__version__ = "1.0.0"
__all__ = [
    "rational_cubic_interpolation",
    "rational_cubic_control_parameter_to_fit_second_derivative_at_left_side",
    "rational_cubic_control_parameter_to_fit_second_derivative_at_right_side",
    "convex_rational_cubic_control_parameter_to_fit_second_derivative_at_left_side",
    "convex_rational_cubic_control_parameter_to_fit_second_derivative_at_right_side",
    "minimum_rational_cubic_control_parameter",
]
