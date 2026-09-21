"""
Example target script for fastercode's YAML-driven run_job() flow.

Note there's no `refactor` import and no decorators here at all - that's
the point. optimise.run_job() dynamically applies tracking to the functions
named in the YAML config, without needing anything hand-written in this file.
"""


def calculate_average(values):
    values = values if hasattr(values, "__len__") else list(values)
    return sum(values) / len(values)


class Scaler:
    def scale(self, values, factor):
        return [value * factor for value in values]


def run_pipeline():
    values = [1, 2, 3, 4, 5]
    average = calculate_average(values)
    scaled = Scaler().scale(values, 2)
    return average, scaled


if __name__ == "__main__":
    print(run_pipeline())
