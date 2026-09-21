"""
Heavy-workload recording node for fastercode speed runs on calculate_average.

In its own module for a mundane reason: _record_test calls pytest.main() inside
the long-lived MCP server process and never drops the test module from
sys.modules, so a second recording in the same server silently re-runs the
FIRST version of an edited test file. A module name that server has not
imported yet gets read fresh. Once _record_test drops stale test modules, this
node can fold back into test_calculate_average.py.

The behavioural suite (generators, iterators, nulls) lives in
test_calculate_average.py. This file exists only to record calls big enough
that the work dominates call overhead.

Usage:
    pytest examples/test_calculate_average_speed.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import target


def test_speed_workload():
    # The small calls keep a candidate honest on shape; the big one is what
    # actually separates the candidates on time.
    assert target.run_pipeline() == (3.0, [2, 4, 6, 8, 10])
    assert target.calculate_average([42]) == 42.0
    assert target.calculate_average((2, 4, 6)) == 4.0
    assert target.calculate_average(list(range(1000))) == 499.5
    assert target.calculate_average(list(range(200_000))) == 99999.5
