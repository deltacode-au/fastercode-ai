"""
MCP server exposing fastercode to a coding agent.

Thin wrapper over agent.py - every tool here is a JSON-shaped call onto the
`agent` class and holds no logic of its own. The tool descriptions carry the
workflow contract, because for an MCP client the description IS the
documentation:

    analyse_function  ->  ask the user which recording strategy to use
    optimise_function ->  measure the candidates, write the report
    list_runs/get_run ->  cite the measurements back to the user

Installed as a standard dependency of fastercode - pip install fastercode-ai
gets you agent mode, no extra required.
Run as:  fastercode-mcp
"""

import json
import os
from typing import List, Optional

from ._agent import agent as _agent
from .adapters import adapter_for_file
from . import storage

_INSTRUCTIONS = """\
fastercode measures whether a code change is actually an improvement. You write \
the candidate code; fastercode swaps it in, replays the function's real recorded \
calls, checks the outputs still match, measures it, and keeps the best candidate \
that is both correct and better. It never calls an LLM.

Workflow for "make this function faster":
  1. analyse_function(file, function) - returns the recording strategies that are
     actually available for this function.
  2. ASK THE USER which one to use. Prefer 'entry' (a real end-to-end replay of
     their script) when analyse_function found an entry point; 'test' reuses their
     own test; 'inputs' needs you to invent representative calls and is the
     weakest evidence. Do not choose silently - the strategy determines what the
     report is evidence OF.
  3. optimise_function(...) with the chosen strategy and your candidate rewrites.
  4. Give the user the returned report_path and summarise what was measured,
     including candidates that were REJECTED - that is the point of the tool.

Never present a rejected candidate as an improvement, and never describe a
measurement the report does not contain.
"""


def _normalise_candidates(candidates):
    # Accepts either a list of code blocks as strings (what a model naturally
    # writes) or as lists of lines (what optimise() consumes internally).
    blocks = []
    for block in candidates or []:
        if isinstance(block, str):
            blocks.append(block.splitlines())
        elif isinstance(block, (list, tuple)):
            blocks.append([str(line) for line in block])
        else:
            raise ValueError(
                f"each candidate must be source code as a string or a list of "
                f"lines, got {type(block).__name__}"
            )
    return blocks


def build_server():
    # The SDK renamed FastMCP to MCPServer in 2.0; the decorator and run()
    # surface this module uses is identical across both.
    try:
        from mcp.server.mcpserver import MCPServer as _Server
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as _Server
        except ImportError:
            raise SystemExit(
                "mcp is a standard fastercode dependency but isn't importable here - "
                "your install looks incomplete. Try:\n"
                "    pip install --upgrade fastercode-ai"
            )

    server = _Server("fastercode", instructions=_INSTRUCTIONS)

    @server.tool()
    def analyse_function(file: str, function: str, language: Optional[str] = None,
                         results_dir: Optional[str] = None) -> str:
        """
        Inspect a function and report which recording strategies can be used to
        measure it. CALL THIS FIRST, then put the options to the user.

        Returns the function's source and parameters, the viable strategies
        (best first, each with the reason it applies), and any previous runs
        whose report could be read instead of re-running.

        language: inferred from the file extension when omitted. Pass it only
            when the extension doesn't settle it. Which strategies exist at all
            depends on the language, so this is reported, never assumed.
        """
        try:
            adapter = adapter_for_file(file, language)
        except ValueError as exc:
            return json.dumps({"status": "error", "reason": str(exc)}, indent=2)
        return json.dumps(
            _agent(adapter=adapter, results_dir=results_dir).analyse(file, function),
            indent=2,
        )

    @server.tool()
    def optimise_function(
        file: str,
        function: str,
        candidates: List[str],
        recording: str = "entry",
        entry_point: Optional[str] = None,
        inputs: Optional[list] = None,
        test: Optional[str] = None,
        objective: str = "speed",
        constraints: Optional[dict] = None,
        mode: str = "independent",
        language: Optional[str] = None,
        results_dir: Optional[str] = None,
    ) -> str:
        """
        Measure your candidate rewrites of `function` against its real recorded
        calls and keep the best correct improvement.

        candidates: your rewrites, each the COMPLETE function source (including
            the `def` line). Every one is swapped in and executed, so each must
            stand alone.
        recording: how the calls to measure against are recorded -
            'entry'  - run the user's script; needs entry_point=<a function that
                       calls the target>. Strongest evidence; prefer it.
            'test'   - run their test; needs test='<path>::<test_name>'.
            'inputs' - you supply the calls; needs inputs=[{"arg": value}, ...].
        objective: 'speed', 'loc' or 'memory'.
        constraints: caps on the two metrics that are NOT the objective, e.g.
            {"memory": {"max_relative": 1.2}} = must not exceed 1.2x the
            baseline's peak memory. Each metric takes exactly one of
            'max' (an absolute ceiling) or 'max_relative' (a multiplier on the
            baseline). A candidate that is correct and meets the objective but
            breaks a constraint is recorded as constraint_violated, not a win.
        mode: 'independent' judges every candidate against the original;
            'iterative' builds on the best so far.
        language: inferred from the file extension when omitted.

        Returns a summary including the baseline, each attempt's verdict and
        measurement, and report_path. A candidate whose outputs differ from the
        original is REJECTED - report that outcome honestly rather than
        presenting it as a win.
        """
        try:
            blocks = _normalise_candidates(candidates)
        except ValueError as exc:
            return json.dumps({"status": "error", "reason": str(exc)}, indent=2)

        # _validate_constraints() raises ValueError on a malformed spec or one
        # aimed at the objective itself - caught below, so a bad constraint
        # comes back as a readable JSON error rather than a server traceback.
        try:
            instance = _agent(objective=objective, constraints=constraints,
                              adapter=adapter_for_file(file, language),
                              results_dir=results_dir)
        except ValueError as exc:
            return json.dumps({"status": "error", "reason": str(exc)}, indent=2)

        try:
            result = instance.optimise_function(
                file, function, candidates=blocks, recording=recording,
                entry_point=entry_point, inputs=inputs, test=test, mode=mode,
            )
        except ValueError as exc:
            return json.dumps({"status": "error", "reason": str(exc)}, indent=2)

        if result.get("status") != "ok":
            return json.dumps(result, indent=2)
        return json.dumps(result["summary"], indent=2)

    @server.tool()
    def list_runs(file: Optional[str] = None, function: Optional[str] = None,
                  results_dir: Optional[str] = None) -> str:
        """
        List optimise runs already on disk, newest first, with the path to each
        report. Use this to point the user at existing evidence.
        """
        return json.dumps(
            _agent(results_dir=results_dir).runs(script=file, function=function), indent=2
        )

    @server.tool()
    def get_run(run_dir: str) -> str:
        """
        Read back a run's full measurements (optimise.json): every attempt, the
        code that was executed, its sha256, and the outputs it produced. Use
        this to quote specific numbers to the user.
        """
        return json.dumps(_agent().read_run(run_dir), indent=2)

    @server.tool()
    def swap_version(file: str, function: str, version: str,
                     language: Optional[str] = None,
                     results_dir: Optional[str] = None) -> str:
        """
        Write a specific recorded version of `function` back into the source
        file. optimise_function already applies the winner, so this is for
        reverting to another measured version, not for normal use.

        language: inferred from the file extension when omitted.
        """
        json_path = storage.find_tracking_json(file, function, json_dir=results_dir)
        if json_path is None:
            return json.dumps({
                "status": "error",
                "reason": f"no tracked data for '{function}' in {file}",
            }, indent=2)
        try:
            backup = storage.apply_swap(json_path, version,
                                        adapter_for_file(file, language))
        except ValueError as exc:
            return json.dumps({"status": "error", "reason": str(exc)}, indent=2)
        return json.dumps({
            "status": "ok", "swapped_version": version,
            "file": os.path.abspath(file), "backup_path": backup,
        }, indent=2)

    return server


def main():
    build_server().run()


if __name__ == "__main__":
    main()
