"""
Internal machinery behind the MCP server. Not a public entry point.

Agent mode is reached through fastercode.mcp_server, and deliberately only
through it: the workflow an agent has to follow - analyse first, ASK THE USER
which recording strategy to use, then report rejected candidates as honestly
as accepted ones - lives in the MCP tool descriptions. A direct import gets
the measurement machinery without any of that contract, which is how a report
ends up being evidence of something nobody chose. Hence the underscore: a
public `fastercode/agent.py` would still satisfy `from fastercode import
agent` as a submodule, whatever __init__.py exports, and fail confusingly at
the call rather than at the import.

`refactor` (core.py) is written for a human editing their own script: decorate
a function, run the script, call optimise(). An agent working from a user's
"make this function faster" has none of that setup - no decorator in the
source, no guarantee the function is even reachable from a script it can run,
and no way to hand-edit anything between steps.

`agent` subclasses `refactor` and adds only that setup: it locates the target,
tracks it, records real calls with whichever strategy suits the code, and
hands the result to the inherited optimise(). It adds NO verification logic of
its own - the agent supplies code, refactor still judges it, and there is
exactly one measurement path in the package.
"""

import contextlib
import json
import os

from . import storage
from .adapters.base import AgentAdapter
from .core import refactor
from .providers.agent_provider import AgentProvider

RECORDING_STRATEGIES = ("entry", "inputs", "test")


class agent(refactor):
    """
    refactor, set up for a caller that writes the code itself.

    Everything inherited from refactor behaves identically; the additions are
    optimise_function() (one call instead of the load/track/patch/record
    sequence) and analyse() (what recording strategies are actually available
    for a given function, so the caller can ask its user rather than guess).
    """

    def __init__(self, objective="speed", adapter=None, constraints=None,
                 results_dir=None):
        # privacy_mode is deliberately not exposed: agent mode makes no LLM
        # call, so there is no payload to anonymize. Passing it through would
        # imply a protection that isn't doing anything here.
        super().__init__(
            objective=objective, adapter=adapter, privacy_mode=False,
            constraints=constraints, results_dir=results_dir,
        )

    # ---------------------------------------------------------------- setup

    @contextlib.contextmanager
    def _agent_provider(self):
        # optimise() stamps llm.get_active_provider() onto its result, which
        # report.py prints. Left alone, an agent run inherits whatever
        # provider llm.py has active (OpenAI by default) and the report claims
        # a model that was never contacted. Swap it for the duration and put
        # the caller's provider back - a human `refactor` instance in this
        # same process must not notice.
        from . import llm
        previous = llm.set_active_provider(AgentProvider())
        try:
            yield
        finally:
            llm.set_active_provider(previous)

    def _globals_anchor(self, function, entry_point):
        # load_script() derives the script's live globals from the __globals__
        # of a name in it, so we need some function to anchor on. The entry
        # point is the natural one; without it, the target itself works - but
        # only when the target is a plain function, since a class has no
        # __globals__.
        if entry_point:
            return entry_point
        if "." in function:
            raise ValueError(
                f"'{function}' is a method, which needs an entry point to reach it: "
                f"pass recording='entry' with entry_point=<function that calls it>"
            )
        return function

    def _prepare(self, script, function, entry_point, fresh):
        # The load/resolve/track/patch sequence, which is otherwise hand-rolled
        # by every caller (see orchestrator.run_worker, which does exactly this
        # for YAML jobs).
        anchor = self._globals_anchor(function, entry_point)
        context = self._adapter.load_script(script, anchor)

        original = self._adapter.resolve_target(context.live_globals, function)
        if original is None:
            raise ValueError(f"target '{function}' not found in {script}")

        wrapped = self.track(original)
        self._adapter.patch_target(context.live_globals, function, wrapped)

        if fresh:
            # Before any call is recorded. Without this, a second run appends
            # to the first run's call history and optimise()'s baseline check
            # aborts on the mismatch - see storage.reset_calls().
            storage.reset_calls(wrapped.refactor_json_path)

        return context, wrapped

    # ------------------------------------------------------------ recording

    def _record_entry(self, context, entry_point):
        self._adapter.call_entry_point(context.live_globals, entry_point)

    def _record_inputs(self, wrapped, inputs):
        # Each entry is one call: a dict is passed as keyword arguments, a
        # list/tuple as positional ones.
        for index, call in enumerate(inputs):
            if isinstance(call, dict):
                wrapped(**call)
            elif isinstance(call, (list, tuple)):
                wrapped(*call)
            else:
                raise ValueError(
                    f"inputs[{index}] must be a dict of keyword arguments or a "
                    f"list of positional ones, got {type(call).__name__}"
                )

    def _record_test(self, script, function, test, wrapped):
        # How a test is located, patched and run is entirely the language's
        # business - pytest and sys.modules are Python's answer, not the
        # workflow's.
        self._adapter.record_test_calls(script, function, test, wrapped)

    # ------------------------------------------------------------- analysis

    def analyse(self, script, function):
        """
        Reports what is actually available for optimising `function`, so the
        caller can put real options to its user instead of guessing.

        Returns a dict with:
          strategies    - viable recording strategies, best first, each with a
                          reason and any arguments it needs
          source        - the function's current source lines
          parameters    - its parameter names
          prior_runs    - existing runs for this function, newest first
        """
        script_path = os.path.abspath(script)
        if not os.path.exists(script_path):
            return {"status": "error", "reason": f"no such file: {script}"}

        if not isinstance(self._adapter, AgentAdapter):
            return {
                "status": "error",
                "reason": (
                    f"agent mode is not supported for this language: "
                    f"{type(self._adapter).__name__} does not implement AgentAdapter, "
                    f"so there is no way to discover how '{function}' can be measured"
                ),
            }

        info = self._adapter.describe_function(script_path, function)
        if info is None:
            return {
                "status": "error",
                "reason": f"'{function}' not found in {script}",
            }

        code = info.source
        parameters = info.parameters

        # The adapter reports what this language can honestly measure for this
        # target; everything below only chooses how to rank and describe it.
        available = self._adapter.supported_strategies(function)
        callers = (
            self._adapter.find_entry_points(script_path, function)
            if "entry" in available else []
        )
        test_nodes = (
            self._adapter.find_tests(script_path, function)
            if "test" in available else []
        )

        strategies = []
        for caller in callers:
            strategies.append({
                "recording": "entry",
                "entry_point": caller,
                "confidence": "preferred",
                "reason": (
                    f"'{caller}' in this file calls '{function}', so every candidate "
                    f"can be verified by replaying the real script end to end"
                ),
            })
        for node_id in test_nodes:
            strategies.append({
                "recording": "test",
                "test": node_id,
                "confidence": "good",
                "reason": (
                    f"'{node_id}' exercises '{function}', so the recorded calls are "
                    f"the ones the project's own test makes"
                ),
            })
        if "inputs" in available:
            strategies.append({
                "recording": "inputs",
                "inputs": None,
                "confidence": "fallback" if (callers or test_nodes) else "only option found",
                "reason": (
                    "you supply representative calls yourself, e.g. "
                    f"inputs=[{{{parameters[0]!r}: ...}}] - needs no entry point, but the "
                    "measurements are only as representative as the inputs you invent"
                    if parameters else
                    "you supply representative calls yourself - needs no entry point"
                ),
            })

        if not strategies and "entry" in available:
            # Nothing was found and captured-input replay isn't available for
            # this target, so an entry point is the only way in. Stated from
            # what this layer actually knows - the adapter already decided
            # WHY 'inputs' is off the table for this language.
            strategies.append({
                "recording": "entry",
                "entry_point": None,
                "confidence": "required",
                "reason": (
                    f"no function in this file calls '{function}', and it cannot be "
                    f"measured from captured inputs, so it needs an entry point"
                ),
            })

        return {
            "status": "ok",
            "script": script_path,
            "function": function,
            "parameters": parameters,
            "source": code,
            "strategies": strategies,
            "prior_runs": self.runs(script=script, function=function),
        }

    # ------------------------------------------------------------- the work

    def optimise_function(self, script, function, *, candidates=None, propose=None,
                          recording="entry", entry_point=None, inputs=None, test=None,
                          mode="independent", tries=None, fresh=True,
                          timeout=None, timeout_margin=0.2):
        """
        Records real calls to `function`, then measures each supplied candidate
        against them and keeps the best one that is both correct and better.

        Supply the code either as `candidates` (a list of code blocks, each a
        list of source lines) or as `propose`, the callback inherited
        optimise() already takes. Nothing is sent to an LLM either way.
        """
        if candidates is None and propose is None:
            raise ValueError(
                "supply candidates=[[...lines...], ...] or propose=<callable> - "
                "agent mode has no LLM to fall back on"
            )
        if candidates is not None and propose is not None:
            raise ValueError("supply candidates= or propose=, not both")
        if recording not in RECORDING_STRATEGIES:
            raise ValueError(
                f"recording must be one of {RECORDING_STRATEGIES}, got {recording!r}"
            )

        if candidates is not None:
            if not candidates or not all(
                isinstance(block, (list, tuple)) and block for block in candidates
            ):
                raise ValueError(
                    "candidates must be a non-empty list of non-empty code blocks, "
                    "each a list of source lines"
                )
            candidate_blocks = [list(block) for block in candidates]
            if tries is None:
                tries = len(candidate_blocks)

            def propose(code, feedback, context):        # noqa: F811 - by design
                return candidate_blocks[(context["try"] - 1) % len(candidate_blocks)]

        if tries is None:
            tries = 5

        if recording == "entry" and not entry_point:
            raise ValueError("recording='entry' requires entry_point=<function name>")
        if recording == "inputs" and not inputs:
            raise ValueError("recording='inputs' requires inputs=[...]")
        if recording == "test" and not test:
            raise ValueError(
                "recording='test' requires test=<test id, in the form this "
                "language's adapter reports from find_tests()>"
            )

        context, wrapped = self._prepare(
            script, function, entry_point if recording == "entry" else None, fresh,
        )

        try:
            if recording == "entry":
                self._record_entry(context, entry_point)
            elif recording == "inputs":
                self._record_inputs(wrapped, inputs)
            else:
                self._record_test(script, function, test, wrapped)
        except Exception as exc:
            return {
                "status": "error",
                "stage": "recording",
                "reason": f"could not record any call to '{function}': {exc}",
                "recovery": [
                    "check the recording strategy matches how this function is reached",
                    "agent.analyse(script, function) lists the strategies that apply",
                ],
            }

        # Captured-input replay reconstructs each recorded call directly, which
        # is what the inputs/test strategies need: neither leaves behind a
        # script that re-makes those calls on its own when run.
        use_captured_inputs = recording != "entry"

        try:
            with self._agent_provider():
                result = self.optimise(
                    wrapped, tries=tries, mode=mode,
                    use_captured_inputs=use_captured_inputs,
                    entry_point=entry_point if recording == "entry" else None,
                    timeout=timeout, timeout_margin=timeout_margin,
                    propose=propose,
                )
        except ValueError as exc:
            return {
                "status": "error",
                "stage": "optimise",
                "reason": str(exc),
                "tracking_json": wrapped.refactor_json_path,
                "recovery": [
                    "re-run with fresh=True so the previous run's recorded calls "
                    "are cleared first",
                    f"or delete {wrapped.refactor_json_path} and record again",
                ],
            }

        result["status"] = "ok"
        result["summary"] = self._summarise(result, script, function, recording)
        return result

    def _summarise(self, result, script, function, recording):
        # optimise()'s result is complete but verbose - every attempt carries
        # its full code, inputs and outputs. This is the part a caller needs to
        # decide what to tell the user.
        objective = result["objective"]
        baseline = {
            "speed": result["original"]["execution_time_ms"],
            "loc": result["original"]["loc_no_comments"],
            "memory": result["original"].get("peak_memory_kb"),
        }[objective]
        winner_metric = result["winner_metric"]

        improvement_pct = None
        if baseline and winner_metric is not None:
            improvement_pct = round((baseline - winner_metric) / baseline * 100, 2)

        return {
            "script": os.path.abspath(script),
            "function": function,
            "objective": objective,
            "recording": recording,
            "recorded_calls": len(result.get("original_inputs") or []),
            "baseline": baseline,
            "winner_version": result["winner_version"],
            "winner_metric": winner_metric,
            "improvement_pct": improvement_pct,
            "applied": result["winner_version"] is not None,
            "attempts": [
                {
                    "try": attempt["try"],
                    "version": attempt["version"],
                    "condition": attempt["condition"],
                    "correct": attempt["correct"],
                    "execution_time_ms": attempt["execution_time_ms"],
                    "loc_no_comments": attempt["loc_no_comments"],
                    "peak_memory_kb": attempt.get("peak_memory_kb"),
                    "code_sha256": attempt["code_sha256"],
                    "error": attempt["error"],
                }
                for attempt in result["attempts"]
            ],
            "report_path": result.get("report_path"),
            "run_dir": result.get("run_dir"),
        }

    # -------------------------------------------------------------- reading

    def runs(self, script=None, function=None):
        """
        Lists optimise runs on disk, newest first, so a caller can point the
        user at an existing report instead of re-running.
        """
        if not os.path.isdir(self._data_dir):
            return []

        found = []
        for hash_name in os.listdir(self._data_dir):
            json_path = os.path.join(self._data_dir, hash_name, "tracking.json")
            if not os.path.exists(json_path):
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as fh:
                    record = json.loads(fh.read())
            except (json.JSONDecodeError, OSError):
                continue

            if function is not None and record.get("function") != function:
                continue
            if script is not None and os.path.abspath(record.get("file", "")) != os.path.abspath(script):
                continue

            runs_dir = os.path.join(self._data_dir, hash_name, "optimise_runs")
            if not os.path.isdir(runs_dir):
                continue
            for run_name in os.listdir(runs_dir):
                run_dir = os.path.join(runs_dir, run_name)
                report = os.path.join(run_dir, "report.html")
                found.append({
                    "run": run_name,
                    "run_dir": run_dir,
                    "function": record.get("function"),
                    "file": record.get("file"),
                    "report_path": report if os.path.exists(report) else None,
                    "complete": os.path.exists(os.path.join(run_dir, "json", "optimise.json")),
                })

        return sorted(found, key=lambda entry: entry["run"], reverse=True)

    def read_run(self, run_dir):
        """Reads back a run's optimise.json - the measurements behind a report."""
        path = os.path.join(run_dir, "json", "optimise.json")
        if not os.path.exists(path):
            return {"status": "error", "reason": f"no optimise.json under {run_dir}"}
        with open(path, "r", encoding="utf-8") as fh:
            return {"status": "ok", "run_dir": run_dir, "optimise": json.loads(fh.read())}
