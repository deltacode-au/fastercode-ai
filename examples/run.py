"""
Example run script for fastercode's YAML-driven job flow.

Usage:
    python examples/run.py
"""

import os
import sys

# Make sure the repo root (where the fastercode package lives) is importable
# regardless of where this script is run from - `python examples/run.py`
# only puts examples/ on sys.path by default, not the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastercode import refactor

optimise = refactor()
exit_code = optimise.run_job("examples/job.yaml")

if exit_code == 0:
    print("Job succeeded - check .fastercode/<hash>/tracking.json for results.")
else:
    print(f"Job failed (exit code {exit_code}) - see stderr above for details.")
