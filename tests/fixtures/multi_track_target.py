"""Fixture that tracks a function of its own, the way the example scripts do."""
from fastercode import refactor

optimise = refactor(objective="speed")


def calculate_average(values):
    total = 0.0

    for value in values:
        total += value

    return total / len(values)


@optimise.track
def summarise(values):
    return {"count": len(values), "largest": max(values)}


def run_pipeline():
    # summarise() runs AFTER the target on purpose: during a replay both are
    # redirected into one single-function record, so whichever writes last is
    # the one that used to end up being compared against the target.
    average = calculate_average([1.0, 2.0, 3.0, 4.0, 5.0])
    summarise([1.0, 2.0, 3.0, 4.0, 5.0])
    return average
