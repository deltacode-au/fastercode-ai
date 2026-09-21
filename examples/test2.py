"""
Deliberately unoptimised block-code example for fastercode.

The code is intentionally inefficient but deterministic.

Potential optimisation opportunities include:

- Recalculating the mean inside a loop
- Recalculating standard deviation inside a loop
- Repeated list copying
- Repeated sorting
- Nested Python loops
- Inefficient membership checks
- Unnecessary intermediate lists
- Repeated polynomial evaluation
- Manual matrix operations
"""

from __future__ import annotations

import math
import random
import time
from fastercode import refactor
optimise = refactor(objective='speed')

RANDOM_SEED = 42
DATA_SIZE = 2_500
WINDOW_SIZE = 30

random.seed(RANDOM_SEED)


# ---------------------------------------------------------------------------
# Input generation
# ---------------------------------------------------------------------------

raw_values: list[float] = []

for index in range(DATA_SIZE):
    random_component = random.random()
    periodic_component = math.sin(index / 25.0)
    secondary_component = math.cos(index / 11.0) * 0.25
    trend_component = index / DATA_SIZE

    value = (
        random_component
        + periodic_component
        + secondary_component
        + trend_component
    )

    raw_values.append(value)


assert len(raw_values) == DATA_SIZE
assert all(math.isfinite(value) for value in raw_values)


# ---------------------------------------------------------------------------
# Start fastercode capture here
# ---------------------------------------------------------------------------

start_time = time.perf_counter()

optimise.start()


# ---------------------------------------------------------------------------
# Calculate the mean inefficiently
# ---------------------------------------------------------------------------

copied_values: list[float] = []

for value in raw_values:
    copied_values.append(value)

total = 0.0

for index in range(len(copied_values)):
    total = total + copied_values[index]

mean_value = total / len(copied_values)

optimise.stop()

# ---------------------------------------------------------------------------
# Calculate variance inefficiently
# ---------------------------------------------------------------------------

squared_differences: list[float] = []

for value in raw_values:
    repeated_total = 0.0

    for repeated_value in raw_values:
        repeated_total = repeated_total + repeated_value

    repeated_mean = repeated_total / len(raw_values)

    difference = value - repeated_mean
    squared_difference = difference * difference

    squared_differences.append(squared_difference)

variance_total = 0.0

for value in squared_differences:
    variance_total = variance_total + value

variance = variance_total / len(squared_differences)
standard_deviation = math.sqrt(variance)


# ---------------------------------------------------------------------------
# Normalise the data inefficiently
# ---------------------------------------------------------------------------

normalised_values: list[float] = []

for value in raw_values:
    current_total = 0.0

    for inner_value in raw_values:
        current_total = current_total + inner_value

    current_mean = current_total / len(raw_values)

    current_squared_differences: list[float] = []

    for inner_value in raw_values:
        difference = inner_value - current_mean
        current_squared_differences.append(difference * difference)

    current_variance_total = 0.0

    for squared_difference in current_squared_differences:
        current_variance_total += squared_difference

    current_variance = current_variance_total / len(raw_values)
    current_standard_deviation = math.sqrt(current_variance)

    if current_standard_deviation == 0.0:
        normalised_value = 0.0
    else:
        normalised_value = (
            value - current_mean
        ) / current_standard_deviation

    normalised_values.append(normalised_value)


# ---------------------------------------------------------------------------
# Moving average with repeated slicing and copying
# ---------------------------------------------------------------------------

moving_averages: list[float] = []

for index in range(len(raw_values)):
    if index < WINDOW_SIZE - 1:
        moving_averages.append(float("nan"))
    else:
        window = raw_values[
            index - WINDOW_SIZE + 1:index + 1
        ]

        copied_window: list[float] = []

        for value in window:
            copied_window.append(value)

        window_total = 0.0

        for value in copied_window:
            window_total += value

        window_average = window_total / len(copied_window)
        moving_averages.append(window_average)


# ---------------------------------------------------------------------------
# Evaluate a polynomial inefficiently
# ---------------------------------------------------------------------------

coefficients = [
    1.0,
    -0.75,
    0.4,
    -0.08,
    0.005,
]

polynomial_values: list[float] = []

for value in raw_values:
    result = 0.0

    for power in range(len(coefficients)):
        coefficient = coefficients[power]
        result += coefficient * (value ** power)

    polynomial_values.append(result)


# ---------------------------------------------------------------------------
# Find the nearest value by sorting every candidate
# ---------------------------------------------------------------------------

target_value = 0.5
distance_candidates: list[tuple[float, float]] = []

for value in polynomial_values:
    distance = abs(value - target_value)
    distance_candidates.append((distance, value))

distance_candidates.sort(
    key=lambda candidate: candidate[0]
)

nearest_value = distance_candidates[0][1]


# ---------------------------------------------------------------------------
# Find the maximum by repeatedly sorting the same data
# ---------------------------------------------------------------------------

maximum_value = polynomial_values[0]

for _ in range(20):
    copied_polynomial_values: list[float] = []

    for value in polynomial_values:
        copied_polynomial_values.append(value)

    copied_polynomial_values.sort()
    maximum_value = copied_polynomial_values[-1]


# ---------------------------------------------------------------------------
# Find unique rounded values with O(n²) list membership checks
# ---------------------------------------------------------------------------

rounded_values: list[float] = []

for value in normalised_values:
    rounded_values.append(round(value, 3))

unique_values: list[float] = []

for value in rounded_values:
    if value not in unique_values:
        unique_values.append(value)


# ---------------------------------------------------------------------------
# Count values above a threshold using an unnecessary list
# ---------------------------------------------------------------------------

values_above_threshold: list[float] = []

for value in polynomial_values:
    if value > 1.0:
        values_above_threshold.append(value)

threshold_count = len(values_above_threshold)


# ---------------------------------------------------------------------------
# Build a pairwise-distance matrix with nested Python loops
# ---------------------------------------------------------------------------

sample_values = normalised_values[:200]
distance_matrix: list[list[float]] = []

for first_value in sample_values:
    row: list[float] = []

    for second_value in sample_values:
        distance = abs(first_value - second_value)
        row.append(distance)

    distance_matrix.append(row)


# ---------------------------------------------------------------------------
# Search every row for its smallest non-zero value by sorting
# ---------------------------------------------------------------------------

nearest_distances: list[float] = []

for row in distance_matrix:
    copied_row: list[float] = []

    for value in row:
        copied_row.append(value)

    copied_row.sort()

    nearest_nonzero = 0.0

    for value in copied_row:
        if value > 0.0:
            nearest_nonzero = value
            break

    nearest_distances.append(nearest_nonzero)


# ---------------------------------------------------------------------------
# Manual matrix multiplication
# ---------------------------------------------------------------------------

matrix_a: list[list[float]] = []

for row_index in range(40):
    row: list[float] = []

    for column_index in range(30):
        source_index = (
            row_index * 30 + column_index
        ) % len(raw_values)

        row.append(raw_values[source_index])

    matrix_a.append(row)


matrix_b: list[list[float]] = []

for row_index in range(30):
    row = []

    for column_index in range(20):
        value = (
            row_index
            + column_index
            + 1
        ) / 100.0

        row.append(value)

    matrix_b.append(row)


matrix_result: list[list[float]] = []

for row_index in range(len(matrix_a)):
    result_row: list[float] = []

    for column_index in range(len(matrix_b[0])):
        cell_total = 0.0

        for shared_index in range(len(matrix_b)):
            left_value = matrix_a[row_index][shared_index]
            right_value = matrix_b[shared_index][column_index]

            cell_total += left_value * right_value

        result_row.append(cell_total)

    matrix_result.append(result_row)


# ---------------------------------------------------------------------------
# Repeated aggregation over the same data
# ---------------------------------------------------------------------------

summary_values: list[float] = []

for _ in range(50):
    repeated_sum = 0.0

    for value in polynomial_values:
        repeated_sum += value

    summary_values.append(
        repeated_sum / len(polynomial_values)
    )


# refactor.end()

elapsed_time = time.perf_counter() - start_time


# ---------------------------------------------------------------------------
# Validation outside the captured block
# ---------------------------------------------------------------------------

assert len(normalised_values) == DATA_SIZE
assert len(moving_averages) == DATA_SIZE
assert len(polynomial_values) == DATA_SIZE

assert math.isclose(
    mean_value,
    sum(raw_values) / len(raw_values),
    rel_tol=1e-12,
    abs_tol=1e-12,
)

assert variance >= 0.0
assert standard_deviation >= 0.0

assert len(distance_matrix) == len(sample_values)
assert all(
    len(row) == len(sample_values)
    for row in distance_matrix
)

assert len(matrix_result) == 40
assert all(
    len(row) == 20
    for row in matrix_result
)

assert all(
    math.isclose(
        value,
        summary_values[0],
        rel_tol=1e-12,
        abs_tol=1e-12,
    )
    for value in summary_values
)


print("Unoptimised block completed")
print("---------------------------")
print(f"Data points: {len(raw_values)}")
print(f"Mean: {mean_value:.10f}")
print(f"Variance: {variance:.10f}")
print(f"Standard deviation: {standard_deviation:.10f}")
print(f"Nearest value to {target_value}: {nearest_value:.10f}")
print(f"Maximum polynomial value: {maximum_value:.10f}")
print(f"Unique rounded values: {len(unique_values)}")
print(f"Values above threshold: {threshold_count}")
print(f"Distance matrix size: {len(distance_matrix)} x {len(distance_matrix[0])}")
print(f"Matrix result size: {len(matrix_result)} x {len(matrix_result[0])}")
print(f"Elapsed time: {elapsed_time:.4f} seconds")