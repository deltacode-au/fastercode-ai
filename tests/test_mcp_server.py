"""
Tests for the MCP server - the only supported way to reach agent mode.

These drive a real server subprocess over stdio, exactly as a client does, so
what is covered is the surface an agent actually calls: the tool list, the
JSON each tool returns, and the workflow contract carried in the descriptions.
"""
import asyncio
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "tests", "fixtures", "sample_target.py")

FASTER = """def calculate_average(values):
    try:
        n = len(values)
    except TypeError:
        values = list(values)
        n = len(values)
    return sum(values) / n"""

WRONG = """def calculate_average(values):
    return 999.0"""


@pytest.fixture
def script(tmp_path):
    target = tmp_path / "sample_target.py"
    shutil.copy(FIXTURE, target)
    return str(target)


def _text(result):
    return "".join(chunk.text for chunk in result.content if getattr(chunk, "text", None))


def _call(tool, arguments):
    """Runs one tool call against a fresh server subprocess, returns parsed JSON."""

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "fastercode.mcp_server"], cwd=REPO,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return json.loads(_text(await session.call_tool(tool, arguments)))

    return asyncio.run(run())


def test_the_expected_tools_are_exposed():
    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "fastercode.mcp_server"], cwd=REPO,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.list_tools()

    names = {tool.name for tool in asyncio.run(run()).tools}
    assert names == {
        "analyse_function", "optimise_function", "list_runs", "get_run", "swap_version",
    }


def test_optimise_functions_description_tells_the_agent_to_report_rejections():
    # The description is the contract - it is the only place an MCP client is
    # told to ask the user and to report rejected candidates honestly.
    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "fastercode.mcp_server"], cwd=REPO,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(run()).tools}
    assert "REJECTED" in tools["optimise_function"].description
    assert "CALL THIS FIRST" in tools["analyse_function"].description


def test_analyse_function_returns_the_available_strategies(script):
    info = _call("analyse_function", {"file": script, "function": "calculate_average"})
    assert info["status"] == "ok"
    assert info["parameters"] == ["values"]
    assert info["strategies"][0]["recording"] == "entry"
    assert info["strategies"][0]["entry_point"] == "run_pipeline"


def test_analyse_function_honours_results_dir(script, tmp_path):
    # Without the parameter, prior_runs is read from the default .fastercode
    # and a caller using a custom results dir is told there is no history.
    results = str(tmp_path / "results")
    _call("optimise_function", {
        "file": script, "function": "calculate_average", "candidates": [FASTER],
        "recording": "entry", "entry_point": "run_pipeline", "results_dir": results,
    })

    scoped = _call("analyse_function", {
        "file": script, "function": "calculate_average", "results_dir": results,
    })
    assert scoped["prior_runs"], "the run just made should be visible"

    unscoped = _call("analyse_function", {
        "file": script, "function": "calculate_average",
    })
    assert unscoped["prior_runs"] == []


def test_optimise_function_measures_candidates_and_writes_a_report(script, tmp_path):
    summary = _call("optimise_function", {
        "file": script,
        "function": "calculate_average",
        "candidates": [FASTER, WRONG],
        "recording": "entry",
        "entry_point": "run_pipeline",
        "results_dir": str(tmp_path / "results"),
    })
    assert summary["recording"] == "entry"
    assert summary["recorded_calls"] == 1
    assert len(summary["attempts"]) == 2

    # The deliberately wrong candidate must be caught, whatever the timings did.
    assert summary["attempts"][1]["correct"] is False
    assert summary["attempts"][1]["condition"] == "rejected"

    assert summary["report_path"] and os.path.exists(summary["report_path"])


def test_candidates_may_be_passed_as_plain_source_strings(script, tmp_path):
    # A model writes code as text, not as a list of lines - both must work.
    summary = _call("optimise_function", {
        "file": script,
        "function": "calculate_average",
        "candidates": [FASTER],
        "recording": "entry",
        "entry_point": "run_pipeline",
        "results_dir": str(tmp_path / "results"),
    })
    assert summary["attempts"][0]["code_sha256"]


def test_a_bad_argument_comes_back_as_json_not_a_crash(script, tmp_path):
    result = _call("optimise_function", {
        "file": script,
        "function": "calculate_average",
        "candidates": [FASTER],
        "recording": "entry",          # entry_point deliberately omitted
        "results_dir": str(tmp_path / "results"),
    })
    assert result["status"] == "error"
    assert "entry_point" in result["reason"]


def test_list_runs_and_get_run_expose_the_measurements(script, tmp_path):
    results_dir = str(tmp_path / "results")
    summary = _call("optimise_function", {
        "file": script,
        "function": "calculate_average",
        "candidates": [FASTER],
        "recording": "entry",
        "entry_point": "run_pipeline",
        "results_dir": results_dir,
    })

    listed = _call("list_runs", {"results_dir": results_dir})
    assert listed and listed[0]["report_path"]

    run = _call("get_run", {"run_dir": summary["run_dir"]})
    assert run["status"] == "ok"
    # No LLM was contacted, and the record must say so.
    assert run["optimise"]["provider"] == {"name": "agent", "model": None}


def test_optimise_function_accepts_constraints(script, tmp_path):
    # Constraints reach the agent only if optimise_function passes them on -
    # without the pass-through this ran as an unconstrained speed job and the
    # caller was never told its cap had been ignored.
    summary = _call("optimise_function", {
        "file": script,
        "function": "calculate_average",
        "candidates": [FASTER],
        "recording": "entry",
        "entry_point": "run_pipeline",
        "objective": "speed",
        "constraints": {"memory": {"max_relative": 1.2}},
        "results_dir": str(tmp_path / "results"),
    })
    assert summary["recorded_calls"] == 1
    assert len(summary["attempts"]) == 1
    # No verdict is asserted: on a function this small the timings are inside
    # the measurement noise, which the suite deliberately does not pin down.


@pytest.mark.parametrize("constraints, expected", [
    ({"speed": {"max_relative": 1.2}}, "already the primary objective"),
    ({"memory": {}}, "exactly one"),
    ({"nonsense": {"max": 1}}, "unknown constraint metric"),
], ids=["targets-the-objective", "neither-max-nor-relative", "unknown-metric"])
def test_a_malformed_constraint_comes_back_as_json_not_a_crash(
    script, tmp_path, constraints, expected
):
    result = _call("optimise_function", {
        "file": script,
        "function": "calculate_average",
        "candidates": [FASTER],
        "recording": "entry",
        "entry_point": "run_pipeline",
        "objective": "speed",
        "constraints": constraints,
        "results_dir": str(tmp_path / "results"),
    })
    assert result["status"] == "error"
    assert expected in result["reason"]


def test_an_unrecognised_file_type_is_reported_not_assumed_to_be_python(tmp_path):
    # Before language dispatch every tool assumed Python, so a file of another
    # language failed with a confusing parse error instead of saying it isn't
    # supported.
    mystery = tmp_path / "thing.made_up"
    mystery.write_text("whatever\n", encoding="utf-8")

    result = _call("analyse_function", {"file": str(mystery), "function": "scale"})
    assert result["status"] == "error"
    assert ".py" in result["reason"]


def test_an_explicit_language_overrides_the_extension(tmp_path):
    # A .txt file that is really Python - the override has to win over the
    # extension guess.
    disguised = tmp_path / "target.txt"
    disguised.write_text(
        "def calculate_average(values):\n"
        "    return sum(values) / len(values)\n"
        "\n"
        "def run_pipeline():\n"
        "    return calculate_average([1, 2, 3])\n",
        encoding="utf-8",
    )

    refused = _call("analyse_function", {
        "file": str(disguised), "function": "calculate_average",
    })
    assert refused["status"] == "error"

    accepted = _call("analyse_function", {
        "file": str(disguised), "function": "calculate_average", "language": "python",
    })
    assert accepted["status"] == "ok"
    assert accepted["parameters"] == ["values"]


def test_swap_version_reports_a_missing_target_cleanly(script, tmp_path):
    result = _call("swap_version", {
        "file": script, "function": "nope", "version": "1",
        "results_dir": str(tmp_path / "results"),
    })
    assert result["status"] == "error"


def test_swap_version_writes_the_requested_version_back(script, tmp_path):
    results_dir = str(tmp_path / "results")
    # Only a rejected candidate, so nothing is applied and the file is left as
    # it was - which keeps the recorded start/end lines valid for the swap.
    _call("optimise_function", {
        "file": script,
        "function": "calculate_average",
        "candidates": [WRONG],
        "recording": "entry",
        "entry_point": "run_pipeline",
        "results_dir": results_dir,
    })
    with open(script, encoding="utf-8") as fh:
        assert "999.0" not in fh.read()

    result = _call("swap_version", {
        "file": script, "function": "calculate_average", "version": "1",
        "results_dir": results_dir,
    })
    assert result["status"] == "ok"
    assert result["swapped_version"] == "1"
    assert os.path.exists(result["backup_path"])

    with open(script, encoding="utf-8") as fh:
        assert "999.0" in fh.read()
