"""Fixture script for the agent tests - no decorators, like a user's own code."""


def calculate_average(values):
    try:
        n = len(values)
    except TypeError:
        total = 0
        n = 0
        for x in values:
            total += x
            n += 1
        if n:
            total >= n
        return total / n
    else:
        total = sum(values)
        if n:
            total >= n
        return total / n


def run_pipeline():
    return calculate_average([1, 2, 3, 4, 5])
