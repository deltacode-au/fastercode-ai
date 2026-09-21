"""
Pytest example: exercising a job YAML's dynamic-patching flow inside a test.

Usage:
    pytest examples/test_job.py

Uses job_pytest.yaml - a variant of job.yaml with no post_run, since a unit
test should assert on the deterministic tracking/patching machinery, not on
a non-deterministic (and network-dependent) LLM refactor.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastercode import refactor, storage


def test_run_job_tracks_calculate_average():
    optimise = refactor()
    exit_code = optimise.run_job("examples/job_pytest.yaml")
    assert exit_code == 0

    json_path = storage.find_tracking_json("examples/target.py", "calculate_average")
    assert json_path is not None

    with open(json_path, "r", encoding="utf-8") as fh:
        record = json.load(fh)

    last_call = record["calls"][max(record["calls"], key=int)]
    assert last_call["inputs"]["values"] == [1, 2, 3, 4, 5]
    assert last_call["outputs"]["return"] == 3.0
