"""
Unit test for examples/shipping_fee.py::shipping_fee_cents.

Doubles as fastercode's recording source - passed as recording='test'. Every
call here is a plain float in, plain float out, so it replays cleanly in a
fresh subprocess.

Usage:
    pytest examples/test_shipping_fee.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shipping_fee import shipping_fee_cents


def test_shipping_fee_always_rounds_up_on_any_fractional_cent():
    assert shipping_fee_cents(distance_km=10, rate_per_km=0.10) == 1.00   # whole cent
    assert shipping_fee_cents(distance_km=3, rate_per_km=0.334) == 1.01   # must round UP
    assert shipping_fee_cents(distance_km=2, rate_per_km=0.301) == 0.61   # must round UP
