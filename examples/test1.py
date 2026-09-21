"""
Deliberately unoptimised Python script for testing fastercode.

The script is intentionally inefficient, but should produce deterministic
outputs. It includes:

- Slow numerical functions
- Repeated calculations
- Nested Python loops
- Unnecessary list construction
- Inefficient lookups
- Redundant sorting
- A class with mutable state
- Global variables
- Assertions and invariants
- Top-level block code
"""

from __future__ import annotations

import math
import random
import time
from fastercode import refactor
optimise = refactor(objective='speed')



RANDOM_SEED = 42
DATA_SIZE = 2_000
WINDOW_SIZE = 25
SCALE_FACTOR = 1.5

def generate_data(size: int) -> list[float]:
    """Generate deterministic pseudo-random data inefficiently."""
    random.seed(RANDOM_SEED)

    values = []

    for index in range(size):
        random_value = random.random()
        periodic_component = math.sin(index / 20.0)
        trend_component = index / size

        value = (
            random_value
            + periodic_component
            + trend_component
        )

        values.append(value)

    return values


def slow_sum(values: list[float]) -> float:
    """Calculate a sum using an unnecessary index-based loop."""
    total = 0.0

    for index in range(len(values)):
        total = total + values[index]

    return total


def slow_mean(values: list[float]) -> float:
    """Calculate a mean while redundantly copying the input."""
    copied_values = []

    for value in values:
        copied_values.append(value)

    total = slow_sum(copied_values)
    return total / len(copied_values)


def slow_variance(values: list[float]) -> float:
    """Calculate population variance with repeated mean computation."""
    squared_differences = []

    for value in values:
        mean_value = slow_mean(values)
        difference = value - mean_value
        squared_difference = difference * difference
        squared_differences.append(squared_difference)

    return slow_sum(squared_differences) / len(squared_differences)


def slow_standard_deviation(values: list[float]) -> float:
    """Calculate standard deviation."""
    variance = slow_variance(values)
    return math.sqrt(variance)


def normalise_values(values: list[float]) -> list[float]:
    """Normalise values with redundant aggregate calculations."""
    return [0.0 if s == 0.0 else (value - m) / s for m, s in [(slow_mean(values), slow_standard_deviation(values))] for value in values] if values else []


def moving_average(
    values: list[float],
    window_size: int,
) -> list[float]:
    """Calculate a moving average using repeated slicing and summation."""
    from functools import reduce
    from operator import add

    nan = float("nan")
    n = len(values)
    start = window_size - 1

    if n < window_size:
        return [nan] * n

    return [nan] * start + [
        reduce(add, values[index - start:index + 1], 0.0) / window_size
        for index in range(start, n)
    ]

@optimise.track
def calculate_pairwise_distance(
    values: list[float],
) -> list[list[float]]:
    """Construct a full pairwise distance matrix using Python loops."""
    n = len(values)
    if n == 0:
        return []
    # Local binding reduces global lookup cost in tight inner loop
    _abs = abs  # localize builtin
    # Convert to tuple for slightly faster iteration in inner loop
    values = tuple(values)
    distance_matrix = [None] * n
    for i, first_value in enumerate(values):
        row = []

        for second_value in values:
            distance = _abs(first_value - second_value)
            row.append(distance)

        distance_matrix[i] = row

    return distance_matrix

optimise.keep(calculate_pairwise_distance, lines=[(137, 139)])


def find_nearest_value(
    values: list[float],
    target: float,
) -> float:
    """Find the nearest value by sorting all candidates."""
    candidates = []

    for value in values:
        distance = abs(value - target)
        candidates.append((distance, value))

    candidates.sort(key=lambda item: item[0])

    return candidates[0][1]


def count_values_above_threshold(
    values: list[float],
    threshold: float,
) -> int:
    """Count values using an unnecessary intermediate list."""
    matches = []

    for value in values:
        if value > threshold:
            matches.append(value)

    return len(matches)


def duplicate_unique_values(values: list[float]) -> list[float]:
    """Find unique values using an O(n²) membership search."""
    unique_values = []

    for value in values:
        if value not in unique_values:
            unique_values.append(value)

    duplicated = []

    for value in unique_values:
        duplicated.append(value)
        duplicated.append(value)

    return duplicated


def slow_polynomial(
    values: list[float],
    coefficients: list[float],
) -> list[float]:
    """Evaluate a polynomial using repeated exponentiation."""
    outputs = []

    for value in values:
        result = 0.0

        for power in range(len(coefficients)):
            coefficient = coefficients[power]
            result = result + coefficient * (value ** power)

        outputs.append(result)

    return outputs


def inefficient_matrix_multiply(
    first_matrix: list[list[float]],
    second_matrix: list[list[float]],
) -> list[list[float]]:
    """Multiply two matrices with pure Python loops."""
    rows = len(first_matrix)
    columns = len(second_matrix[0])
    shared_dimension = len(second_matrix)

    result = []

    for row_index in range(rows):
        result_row = []

        for column_index in range(columns):
            total = 0.0

            for shared_index in range(shared_dimension):
                left_value = first_matrix[row_index][shared_index]
                right_value = second_matrix[shared_index][column_index]
                total = total + left_value * right_value

            result_row.append(total)

        result.append(result_row)

    return result


class SignalProcessor:
    """A deliberately inefficient stateful signal processor."""

    def __init__(
        self,
        scale_factor: float,
        offset: float = 0.0,
    ) -> None:
        self.scale_factor = scale_factor
        self.offset = offset
        self.processed_count = 0
        self.history: list[list[float]] = []

    def scale_values(
        self,
        values: list[float],
    ) -> list[float]:
        """Scale values while repeatedly reading instance attributes."""
        scaled_values = []

        for value in values:
            scaled_value = (
                value * self.scale_factor
            ) + self.offset

            scaled_values.append(scaled_value)
            self.processed_count = self.processed_count + 1

        self.history.append(scaled_values)

        return scaled_values

    def repeatedly_find_maximum(
        self,
        values: list[float],
    ) -> float:
        """Find a maximum after repeatedly sorting the same input."""
        maximum = values[0]

        for _ in range(10):
            copied_values = []

            for value in values:
                copied_values.append(value)

            copied_values.sort()
            maximum = copied_values[-1]

        return maximum

    def assert_internal_state(self) -> None:
        """Assert class invariants."""
        assert self.scale_factor > 0.0
        assert self.processed_count >= 0
        assert len(self.history) >= 0


def run_pipeline() -> dict[str, object]:
    """Run the deliberately inefficient processing pipeline."""
    start_time = time.perf_counter()

    raw_values = generate_data(DATA_SIZE)

    assert len(raw_values) == DATA_SIZE
    assert all(math.isfinite(value) for value in raw_values)

    processor = SignalProcessor(
        scale_factor=SCALE_FACTOR,
        offset=0.25,
    )

    scaled_values = processor.scale_values(raw_values)

    assert len(scaled_values) == len(raw_values)
    assert processor.processed_count == len(raw_values)

    raw_mean = slow_mean(raw_values)
    raw_variance = slow_variance(raw_values)
    raw_standard_deviation = slow_standard_deviation(raw_values)

    assert raw_variance >= 0.0
    assert raw_standard_deviation >= 0.0
    assert math.isclose(
        raw_standard_deviation ** 2,
        raw_variance,
        rel_tol=1e-9,
        abs_tol=1e-9,
    )

    rolling_average = moving_average(
        scaled_values,
        WINDOW_SIZE,
    )

    assert len(rolling_average) == len(scaled_values)

    polynomial_values = slow_polynomial(
        raw_values,
        coefficients=[1.0, -0.5, 0.25, -0.01],
    )

    assert len(polynomial_values) == len(raw_values)

    nearest_to_zero = find_nearest_value(
        polynomial_values,
        target=0.0,
    )

    threshold_count = count_values_above_threshold(
        polynomial_values,
        threshold=1.0,
    )

    maximum_value = processor.repeatedly_find_maximum(
        polynomial_values
    )

    processor.assert_internal_state()

    small_sample = raw_values[:150]

    pairwise_distances = calculate_pairwise_distance(
        small_sample
    )

    assert len(pairwise_distances) == len(small_sample)
    assert all(
        len(row) == len(small_sample)
        for row in pairwise_distances
    )

    for index in range(len(small_sample)):
        assert pairwise_distances[index][index] == 0.0

    first_matrix = [
        raw_values[0:10],
        raw_values[10:20],
        raw_values[20:30],
    ]

    second_matrix = []

    for column_index in range(4):
        row = []

        for row_index in range(10):
            value = (
                row_index
                + column_index
                + 1
            ) / 10.0

            row.append(value)

        second_matrix.append(row)

    transposed_second_matrix = []

    for column_index in range(4):
        transposed_row = []

        for row_index in range(10):
            transposed_row.append(
                second_matrix[column_index][row_index]
            )

        transposed_second_matrix.append(transposed_row)

    correctly_shaped_second_matrix = []

    for row_index in range(10):
        row = []

        for column_index in range(4):
            row.append(
                transposed_second_matrix[column_index][row_index]
            )

        correctly_shaped_second_matrix.append(row)

    matrix_result = inefficient_matrix_multiply(
        first_matrix,
        correctly_shaped_second_matrix,
    )

    assert len(matrix_result) == 3
    assert all(len(row) == 4 for row in matrix_result)

    elapsed_time = time.perf_counter() - start_time

    return {
        "raw_mean": raw_mean,
        "raw_variance": raw_variance,
        "raw_standard_deviation": raw_standard_deviation,
        "nearest_to_zero": nearest_to_zero,
        "threshold_count": threshold_count,
        "maximum_value": maximum_value,
        "rolling_average": rolling_average,
        "matrix_result": matrix_result,
        "processed_count": processor.processed_count,
        "elapsed_time_seconds": elapsed_time,
    }


if __name__ == "__main__":
    results = run_pipeline()
    optimise.submit(calculate_pairwise_distance)

    print("Pipeline complete")
    print("-----------------")
    print(f"Mean: {results['raw_mean']:.8f}")
    print(f"Variance: {results['raw_variance']:.8f}")
    print(
        "Standard deviation: "
        f"{results['raw_standard_deviation']:.8f}"
    )
    print(
        "Nearest polynomial value to zero: "
        f"{results['nearest_to_zero']:.8f}"
    )
    print(
        "Values above threshold: "
        f"{results['threshold_count']}"
    )
    print(
        "Maximum polynomial value: "
        f"{results['maximum_value']:.8f}"
    )
    print(
        "Processed values: "
        f"{results['processed_count']}"
    )
    print(
        "Matrix result: "
        f"{results['matrix_result']}"
    )
    print(
        "Elapsed time: "
        f"{results['elapsed_time_seconds']:.4f} seconds"
    )