"""
Unit tests for examples/target.py::calculate_average.

Doubles as fastercode's recording source. test_pipeline_and_representative_calls
is the node passed as recording='test' - every argument it passes MUST be
plain-JSON-native, because optimise() rebuilds each recorded call in a fresh
subprocess and aborts on anything it cannot reconstruct (core.py:484-495).
The generator, iterator and null cases therefore live in their own tests: they
assert behaviour fastercode structurally cannot replay, and they are what keeps a
shorter candidate honest. Generators cannot be reconstructed from JSON at all;
a call that raises is never recorded in the first place, because the exception
leaves the tracked wrapper before record_function_call runs (core.py:258-282).

Calls go through `target.<name>` on purpose - during a recording run fastercode
patches the tracked wrapper onto the imported module, so a name bound at import
time would miss it.

Usage:
    pytest examples/test_calculate_average.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import target


def test_pipeline_and_representative_calls():
    # The real end-to-end call - the same one recording='entry' would capture.
    assert target.run_pipeline() == (3.0, [2, 4, 6, 8, 10])
    assert target.calculate_average([1, 2, 3, 4, 5]) == 3.0
    assert target.calculate_average([42]) == 42.0
    assert target.calculate_average((2, 4, 6)) == 4.0
    assert target.calculate_average([-1.5, 1.5]) == 0.0
    assert target.calculate_average(list(range(1000))) == 499.5
    # The small calls above are dominated by call overhead. Speed runs need one
    # workload where the work itself costs something, or the measurement is
    # noise and every candidate looks identical.
    assert target.calculate_average(list(range(100_000))) == 49999.5


def test_accepts_a_generator():
    assert target.calculate_average(x for x in [1, 2, 3]) == 2.0


def test_accepts_an_iterator():
    assert target.calculate_average(iter([2, 4, 6])) == 4.0


def test_none_as_the_argument_raises():
    # list(None) - not iterable. The original's len(None) raised TypeError too,
    # then its except branch hit the same failure, so every variant agrees.
    with pytest.raises(TypeError):
        target.calculate_average(None)


@pytest.mark.parametrize("values", [[1, None, 3], [None], [None, None]])
def test_null_inside_values_raises(values):
    # sum() reaches int + None. A list with holes in it is a bug in the caller,
    # not something to paper over with a default.
    with pytest.raises(TypeError):
        target.calculate_average(values)


def test_empty_input_raises_zero_division():
    with pytest.raises(ZeroDivisionError):
        target.calculate_average([])
