"""Fixture whose output legitimately contains NaN - no decorators, like a user's own code."""


def moving_average(values, window):
    averages = []

    for index in range(len(values)):
        if index + 1 < window:
            averages.append(float("nan"))
        else:
            total = 0.0

            for value in values[index - window + 1:index + 1]:
                total += value

            averages.append(total / window)

    return averages


def run_pipeline():
    return moving_average([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 3)
