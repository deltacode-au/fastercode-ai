"""
Example target for fastercode's test-recording strategy.

No decorator, no entry point - this function is only ever reached through
its own test file, which is exactly the recording='test' case.
"""
import math


def shipping_fee_cents(distance_km, rate_per_km):
    cents = distance_km * rate_per_km * 100
    return -(-cents // 1) / 100
