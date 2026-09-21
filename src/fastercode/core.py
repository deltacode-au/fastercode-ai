import copy
import datetime
import functools
import inspect
import json
import math
import os

from . import storage
from .adapters.base import ReplayError, ReplayTimeoutError
from .adapters.python_adapter import PythonAdapter

_REPLAY_STARTUP_ALLOWANCE_SECONDS = 3.0
_REPLAY_TIMEOUT_FLOOR_SECONDS = 5.0

_VALID_METRICS = ("speed", "loc", "memory")


def _validate_constraints(constraints, objective):
    # constraints is "a second component of objective": caps on the two
    # metrics NOT chosen as the primary objective (e.g. objective="speed"
    # can constrain "loc"/"memory", but not "speed" itself - that's already
    # what's being minimized). Each constraint is either an absolute
    # ceiling ("max") or relative to the recorded baseline ("max_relative",
    # a multiplier - e.g. 1.2 = must not exceed 120% of baseline).
    if not constraints:
        return {}
    normalized_objective = (objective or "speed").lower()
    validated = {}
    for metric, spec in constraints.items():
        if metric not in _VALID_METRICS:
            raise ValueError(
                f"unknown constraint metric {metric!r} - must be one of {_VALID_METRICS}"
            )
        if metric == normalized_objective:
            raise ValueError(
                f"constraints cannot target {metric!r} - it's already the primary "
                f"objective, so it's already being minimized; constraints only "
                f"apply to the other metrics"
            )
        has_max = "max" in spec
        has_relative = "max_relative" in spec
        if has_max == has_relative:
            raise ValueError(
                f"constraint {metric!r} must specify exactly one of 'max' or "
                f"'max_relative', got {spec!r}"
            )
        validated[metric] = dict(spec)
    return validated


def _aggregate_peak_memory_kb(invocations):
    # Peak memory is a high-water-mark concept, not a cumulative cost like
    # execution time - the worst single call in the run, not their sum.
    return max((inv.peak_memory_kb for inv in invocations), default=0.0)


def _outputs_match(recorded, replayed):
    # "Did this code produce the same outputs?" - which plain == answers
    # correctly today only by accident. nan != nan, but both sides of every
    # comparison here are parsed by json.loads, json.decoder holds ONE
    # module-level NaN object that every NaN token decodes to, and list/dict
    # equality checks identity before value - so NaN-containing outputs
    # compare equal via that shortcut. None of which is a documented promise.
    #
    # This makes the intent explicit instead: positional NaN counts as
    # reproduced because a function that pads with NaN reproduces itself.
    # Behaviour is unchanged; the point is that it no longer depends on a
    # CPython implementation detail holding. Every other comparison stays
    # exactly as strict, including the float equality that makes
    # accumulation-order drift a real rejection.
    if isinstance(recorded, float) and isinstance(replayed, float):
        if math.isnan(recorded) and math.isnan(replayed):
            return True
        return recorded == replayed
    if isinstance(recorded, (list, tuple)) and isinstance(replayed, (list, tuple)):
        return len(recorded) == len(replayed) and all(
            _outputs_match(left, right) for left, right in zip(recorded, replayed)
        )
    if isinstance(recorded, dict) and isinstance(replayed, dict):
        return recorded.keys() == replayed.keys() and all(
            _outputs_match(recorded[key], replayed[key]) for key in recorded
        )
    return recorded == replayed


_METRIC_UNITS = {"speed": "ms", "loc": "lines", "memory": "KB"}


def _describe_attempt(
    attempt, constraints, baseline_values, current_values, best_metric,
    metric_label, timeout_seconds,
):
    # One text block per try, for iterative mode's accumulating feedback:
    # what was tried, whether it was kept, and why - so the next prompt
    # can see concretely what already failed (and why) instead of just a
    # number to beat.
    header = f"Attempt {attempt['try']}"

    if attempt["code"] is None:
        return f"{header} - REJECTED (error before a candidate was produced: {attempt['error']})"

    code = "\n".join(attempt["code"])

    if attempt["condition"] == "accepted":
        return f"{header} - ACCEPTED (new best {metric_label} = {best_metric}):\n{code}"

    if attempt["condition"] == "constraint_violated":
        violations = []
        for metric, spec in constraints.items():
            cap = spec["max"] if "max" in spec else baseline_values[metric] * spec["max_relative"]
            actual = current_values[metric]
            if actual > cap:
                unit = _METRIC_UNITS[metric]
                violations.append(f"{metric} constraint: {actual:.1f} {unit} > {cap:.1f} {unit} cap")
        reason = "; ".join(violations) if violations else "a constraint"
        return f"{header} - REJECTED (violated {reason}):\n{code}"

    if attempt["condition"] == "timeout":
        return f"{header} - REJECTED (timed out after {timeout_seconds:.1f}s):\n{code}"

    if attempt["correct"] is None:
        return f"{header} - REJECTED (error while testing candidate: {attempt['error']}):\n{code}"

    if attempt["correct"] is False:
        return f"{header} - REJECTED (produced incorrect output):\n{code}"

    return (
        f"{header} - REJECTED (correct, but not better than current best "
        f"{metric_label} = {best_metric}):\n{code}"
    )


class refactor:

    _JSON_FILENAME = "refactor.json"

    def __init__(self, objective=None, adapter=None, privacy_mode=True, constraints=None,
                 results_dir=None):

        self.objective = objective  # speed, loc, memory
        self._adapter = adapter if adapter is not None else PythonAdapter()
        self.privacy_mode = bool(privacy_mode)
        self.constraints = _validate_constraints(constraints, objective)
        # Resolved once, eagerly, at construction time (same as privacy_mode
        # above) - precedence is this explicit arg, then FASTERCODE_RESULTS_DIR,
        # then the hardcoded default; see storage.resolve_data_dir(). Resolving
        # now (rather than at each use) means a later env var change can't
        # cause inconsistent behavior partway through one instance's life.
        self._data_dir = storage.resolve_data_dir(results_dir)

        # Also, creates ID for test case.
        # Checks if any manual code has changed since the last time
        # this class was instantiated.
        # If no code was manually changed in the start/stop, the ID get's passed into the same group.

    def _capture_scope(self, frame):
        merged = {}
        merged.update(frame.f_globals)
        merged.update(frame.f_locals)
        return {name: value for name, value in merged.items() if value is not self}

    def _deep_copy_scope(self, scope):
        copied = {}
        for name, value in scope.items():
            if type(value).__eq__ is object.__eq__:
                # No custom equality (e.g. functions, classes, plain objects):
                # a real deep copy would get a distinct identity and compare
                # unequal to the live object even when nothing changed, so
                # keep the live reference and rely on identity comparison.
                copied[name] = value
                continue
            try:
                copied[name] = copy.deepcopy(value)
            except Exception:
                try:
                    copied[name] = copy.copy(value)
                except Exception:
                    copied[name] = value
        return copied

    def _diff_scopes(self, before, after):
        changed = {}
        for name, after_value in after.items():
            if name not in before:
                changed[name] = after_value
                continue
            try:
                equal = bool(before[name] == after_value)
            except Exception:
                equal = False
            if not equal:
                changed[name] = after_value
        return changed

    def _append_entry(self, entry):
        os.makedirs(self._data_dir, exist_ok=True)
        path = os.path.join(self._data_dir, self._JSON_FILENAME)
        entries = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
            if raw:
                entries = json.loads(raw)
                if not isinstance(entries, list):
                    entries = []
        next_id = 1 + max((e.get("id", 0) for e in entries), default=0)
        entries.append({"id": next_id, **entry})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, cls=storage.RefactorJSONEncoder, indent=2)

    def start(self):
        # Determines where the snipped code starts.
        # Starts capturing variables from here.
        # Inputs, outputs, global variables, function variables etc.
        if getattr(self, "_start_snapshot", None) is not None:
            raise RuntimeError("start() called again before a matching stop()")

        caller_frame = inspect.currentframe().f_back
        try:
            self._start_snapshot = self._deep_copy_scope(self._capture_scope(caller_frame))
            self._start_meta = {
                "file": caller_frame.f_code.co_filename,
                "scope_name": caller_frame.f_code.co_name,
                "start_line": caller_frame.f_lineno,
                "timestamp_start": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        finally:
            del caller_frame

    def stop(self):
        # Determines where the snipped code ends.
        # Writes data to a JSON.
        # Stops recording variables.
        if getattr(self, "_start_snapshot", None) is None:
            raise RuntimeError("stop() called without a preceding start()")

        caller_frame = inspect.currentframe().f_back
        try:
            after_scope = self._capture_scope(caller_frame)
            variables = self._diff_scopes(self._start_snapshot, after_scope)

            entry = dict(self._start_meta)
            entry["objective"] = self.objective
            entry["stop_line"] = caller_frame.f_lineno
            entry["timestamp_stop"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            entry["variables"] = variables

            self._append_entry(entry)
        finally:
            del caller_frame
            self._start_snapshot = None
            self._start_meta = None

    def track(self, func):
        # Decorator (usable as @optimise.track or optimise.track(func)):
        # captures inputs/outputs for every call automatically, so callers
        # don't need to manually bracket the call with start()/stop().
        #
        # Return value convention: a tuple return is split into
        # "return_0", "return_1", ... ; any other value uses "return".
        source_location = self._adapter.get_source_location(func)
        binder = self._adapter.make_argument_binder(func)

        meta = {
            "objective": self.objective,
            "constraints": self.constraints,
            "function": source_location.qualified_name,
            "file": source_location.file,
            "start_line": source_location.start_line,
            "end_line": source_location.end_line,
            "code": source_location.code,
        }

        # optimise()'s entry-script replay redirects every track()-recorded
        # call in the replay subprocess into a dedicated, throwaway
        # verification file via this env var, so re-running the entry
        # script to check a candidate never writes into the real,
        # persistent tracking.json - regardless of which .fastercode/<hash>/
        # this function's current code would otherwise land in.
        override = os.environ.get("FASTERCODE_REPLAY_JSON_PATH")

        # That redirect points EVERY tracked function in the replay
        # subprocess at one file, and that file holds one function's record.
        # A script that does its own @track (as the examples do) would then
        # overwrite the record for the function being optimised with its own,
        # and the harness would read those calls back as if they belonged to
        # the target - comparing one function's outputs against another's.
        # So during a replay only the target records; everything else calls
        # straight through, which still keeps it out of the real, persistent
        # tracking.json.
        replay_target = os.environ.get("FASTERCODE_REPLAY_TARGET")
        skip_recording = bool(
            override
            and replay_target
            and source_location.qualified_name != replay_target
        )

        if override:
            json_path = override
        else:
            digest = self._adapter.content_hash(source_location)
            json_path = os.path.join(self._data_dir, digest[:16], "tracking.json")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if skip_recording:
                return func(*args, **kwargs)

            inputs = binder(args, kwargs)

            keep_spec = getattr(wrapper, "refactor_keep_spec", None)
            requested_variables = keep_spec["variables"] if keep_spec else []
            requested_lines = keep_spec["lines"] if keep_spec else []

            invocation = self._adapter.invoke_and_capture(func, args, kwargs, requested_variables)
            kept_lines = self._adapter.extract_kept_lines(source_location, requested_lines)

            call_data = {
                "execution_time_ms": invocation.execution_time_ms,
                "peak_memory_kb": invocation.peak_memory_kb,
                "inputs": inputs,
                "outputs": invocation.outputs,
            }

            storage.record_function_call(
                json_path, meta, call_data,
                kept_lines=kept_lines, kept_variables=invocation.captured_variables,
                keep_spec=keep_spec,
            )

            return invocation.result

        wrapper.refactor_json_path = json_path
        wrapper.refactor_keep_spec = None

        return wrapper

    def submit(self, func, feedback=None):
        # Sends a .track-decorated function's tracked JSON off for LLM
        # processing. Called explicitly by the user (e.g. optimise.submit(compute))
        # whenever/wherever they choose - not triggered automatically by track().
        # `feedback` is optional plain text appended to the prompt (used by
        # optimise()'s iterative mode); ordinary callers omit it.
        json_path = getattr(func, "refactor_json_path", None)
        if json_path is None:
            raise ValueError("submit() requires a function decorated with @track")
        if not os.path.exists(json_path):
            raise ValueError(
                f"no tracked calls recorded yet for this function (expected {json_path})"
            )

        with open(json_path, "r", encoding="utf-8") as fh:
            record = json.loads(fh.read())

        from . import llm

        if not self.privacy_mode:
            return llm.process(json_path, record, self._adapter, feedback=feedback)

        # privacy_mode: anonymize before it ever reaches llm.process() (the
        # only network call site in the whole codebase), then reverse it on
        # the response before anything is written back to refactored.json.
        # llm.py itself stays completely unaware this is happening - it only
        # ever sees the (already-anonymized) record dict.
        anon = self._adapter.anonymize_record(record)
        presented = dict(record)
        presented["code"] = anon.anonymized_code
        presented["keep_variables"] = anon.anonymized_keep_variables
        presented["keep_LOC"] = anon.anonymized_keep_loc

        lines = llm.process(json_path, presented, self._adapter, feedback=feedback)
        version = storage.latest_refactored_version(json_path)
        storage.write_privacy_mapping(json_path, version, anon.mapping)

        real_lines = self._adapter.deanonymize_lines(lines, anon.mapping)
        storage.overwrite_refactored_version_code(json_path, version, real_lines)
        return real_lines

    def swap_code(self, func, version):
        # Swaps a refactored.json version's code into the original source
        # file where the tracked function actually lives, backing up the
        # whole original file first (destructive - see plan for the known
        # risk if start_line/end_line have drifted since the last tracked call).
        json_path = getattr(func, "refactor_json_path", None)
        if json_path is None:
            raise ValueError("swap_code() requires a function decorated with @track")
        backup_path = storage.apply_swap(json_path, version, self._adapter)

        # Keep the in-memory spec in sync with what apply_swap persisted,
        # so a subsequent call in this same process keeps watching the
        # correct (now-shifted) lines without needing keep() called again.
        with open(json_path, "r", encoding="utf-8") as fh:
            record = json.loads(fh.read())
        keep_spec = record.get("keep_spec")
        if keep_spec:
            func.refactor_keep_spec = {
                "variables": keep_spec.get("variables", []),
                "lines": [tuple(pair) for pair in keep_spec.get("lines", [])],
            }

        return backup_path

    def keep(self, func, variables=None, lines=None):
        # Registers which local variables and/or source line ranges to
        # capture on every future call of a .track-decorated function.
        # Actual capture happens inside track()'s wrapper on each subsequent
        # call. `lines` is a list of (start, end) pairs, e.g. [(10, 12)] -
        # a single line is expressed as (n, n).
        if getattr(func, "refactor_json_path", None) is None:
            raise ValueError("keep() requires a function decorated with @track")

        line_ranges = []
        for pair in (lines or []):
            if len(pair) != 2:
                raise ValueError(f"lines entries must be (start, end) pairs, got {pair!r}")
            start, end = pair
            if start > end:
                raise ValueError(f"invalid range {pair!r}: start must be <= end")
            line_ranges.append((start, end))

        func.refactor_keep_spec = {
            "variables": list(variables) if variables else [],
            "lines": line_ranges,
        }

    def run_job(self, yaml_path):
        # Runs a single YAML-configured job (see orchestrator.py): imports
        # and dynamically tracks functions in a target script by name (no
        # decorators needed in that script), runs its entry point, then
        # performs any configured submit/swap post_run steps. Runs as an
        # isolated subprocess - self's own objective/state isn't used, since
        # the job builds its own refactor instance from the YAML instead.
        from . import orchestrator
        return orchestrator.run_job(yaml_path)

    def optimise(self, func, tries=5, mode="iterative", use_captured_inputs=False,
                 entry_point=None, timeout=None, timeout_margin=0.2, propose=None):
        # `propose` inverts who writes the code. Passed a callable, optimise()
        # asks IT for each candidate instead of calling an LLM - for callers
        # that already have a model of their own (an IDE agent), so the
        # client's API key isn't paying for a second one. Signature:
        #
        #     propose(code, feedback, context) -> list[str]
        #
        # Everything downstream is unchanged: the returned lines are
        # registered as an ordinary refactored.json version and then swapped,
        # replayed, compared and scored exactly like an LLM's. The caller
        # supplies code; this method decides whether it's any good.
        #
        # Consequences of no LLM call: no credentials are needed, and
        # privacy_mode is vacuous (nothing leaves the machine, so there is
        # nothing to anonymize) - it's ignored rather than silently applied,
        # so the callback sees real identifiers rather than pseudonyms.
        # Automatically submits/swaps/measures up to `tries` candidates and
        # settles on whichever is both correct and best for `self.objective`
        # ("speed"/"loc"/"memory" - fastest, fewest lines, or lowest peak
        # memory). `self.constraints` (set at __init__/track() time, "a
        # second component of objective") can additionally cap the OTHER
        # two metrics - a candidate that's correct and improves the primary
        # objective still gets rejected (condition "constraint_violated")
        # if it exceeds a constrained metric's ceiling. Swapping code into
        # the source file doesn't hot-reload the already-imported function
        # in this process, so every candidate is measured by replaying it
        # in a fresh subprocess.
        #
        # Both modes check the function's ENTIRE recorded call history, not
        # just the most recent call - they only differ in how each call
        # gets replayed. By default (use_captured_inputs=False), correctness
        # is checked by re-running the real entry script fresh once and
        # observing whatever calls it naturally makes (see
        # LanguageAdapter.replay_entry). Set use_captured_inputs=True to
        # instead reconstruct and directly invoke the function once per
        # historical call, using each call's own stored inputs (see
        # LanguageAdapter.replay_call) - recommended when the tracked
        # function is a plain function/method with JSON-native inputs and
        # outputs and no surrounding class/broader-object state to
        # reconstruct, since it's simpler and faster than requiring a real,
        # deterministically-replayable entry script.
        #
        # `entry_point`, only meaningful when use_captured_inputs=False: for
        # job.yaml-driven targets (only ever dynamically patched onto an
        # undecorated script, never @track-decorated in source), this must
        # be the job's entry_point so replay_entry can re-patch and call it,
        # instead of assuming the script tracks itself when run standalone.
        json_path = getattr(func, "refactor_json_path", None)
        if json_path is None:
            raise ValueError("optimise() requires a function decorated with @track")
        if not os.path.exists(json_path):
            raise ValueError(
                f"no tracked calls recorded yet for this function (expected {json_path})"
            )
        if mode not in ("iterative", "independent"):
            raise ValueError(f"mode must be 'iterative' or 'independent', got {mode!r}")
        if not isinstance(tries, int) or isinstance(tries, bool) or tries < 1:
            raise ValueError(f"tries must be a positive integer, got {tries!r}")

        with open(json_path, "r", encoding="utf-8") as fh:
            record = json.loads(fh.read())
        calls = record.get("calls") or {}
        if not calls:
            raise ValueError("optimise() requires at least one recorded call")

        historical_calls = [calls[key] for key in sorted(calls, key=int)]

        if use_captured_inputs:
            # Checked against ALL historical calls, not just the most
            # recent - a bad value anywhere in the history aborts before
            # anything changes, same as before, just no longer scoped to
            # a single call.
            for call in historical_calls:
                try:
                    json.dumps(call["inputs"])
                    json.dumps(call["outputs"])
                except TypeError as exc:
                    raise ValueError(
                        f"optimise() requires every tracked call's inputs/outputs to "
                        f"be plain JSON-serializable, since they must survive a round "
                        f"trip into a fresh subprocess for replay: {exc}"
                    )
                if (
                    storage.contains_encoder_placeholder(call["inputs"])
                    or storage.contains_encoder_placeholder(call["outputs"])
                ):
                    raise ValueError(
                        "optimise() requires every tracked call's inputs/outputs to "
                        "be plain JSON-native values - at least one recorded call has "
                        "a value (e.g. an instance method's 'self') that could not be "
                        "JSON-serialized and was stored as a repr placeholder instead, "
                        "which can't be reconstructed for a subprocess replay. Stateful "
                        "instance methods aren't supported by use_captured_inputs=True."
                    )
            replay_timeout_seconds = None
        else:
            # Based on the calls' own recorded (real-usage) execution times,
            # not a replay that hasn't happened yet - avoids a chicken-and-egg
            # problem, and matches "the original execution time" directly.
            baseline_total_seconds = sum(
                (c.get("execution_time_ms") or 0) for c in historical_calls
            ) / 1000.0
            replay_timeout_seconds = timeout if timeout is not None else (
                _REPLAY_STARTUP_ALLOWANCE_SECONDS + max(
                    baseline_total_seconds * (1 + timeout_margin),
                    _REPLAY_TIMEOUT_FLOOR_SECONDS,
                )
            )

        objective = (record.get("objective") or "speed").lower()
        if objective not in _VALID_METRICS:
            objective = "speed"
        constraints = record.get("constraints") or {}
        hash_dir = os.path.dirname(json_path)
        run_dirs = storage.make_optimise_run_dir(json_path)

        original_snapshot = storage.snapshot_state(json_path)
        original_record = original_snapshot["record"]
        original_code = original_record.get("code") or []

        if use_captured_inputs:
            try:
                baseline_invocations = [
                    self._adapter.replay_call(
                        original_record["file"], original_record["function"],
                        call["inputs"], run_dirs["json_dir"], f"baseline{idx}",
                    )
                    for idx, call in enumerate(historical_calls)
                ]
            except ReplayError as exc:
                raise ValueError(
                    f"optimise() aborted: replaying the current unmodified code "
                    f"failed - nothing was changed and no LLM calls were made: {exc}"
                )
            if any(
                not _outputs_match(hist["outputs"], inv.outputs)
                for inv, hist in zip(baseline_invocations, historical_calls)
            ):
                raise ValueError(
                    "optimise() aborted: replaying the current unmodified code did "
                    f"not reproduce all {len(historical_calls)} recorded call(s) - "
                    "nothing was changed and no LLM calls were made"
                )
            baseline_execution_time_ms = sum(inv.execution_time_ms for inv in baseline_invocations)
            baseline_memory_kb = _aggregate_peak_memory_kb(baseline_invocations)
            original_inputs_out = [c["inputs"] for c in historical_calls]
            original_outputs_out = [c["outputs"] for c in historical_calls]
        else:
            verify_path = os.path.join(run_dirs["json_dir"], "replay_baseline_verify.json")
            try:
                baseline_invocations = self._adapter.replay_entry(
                    original_record["file"], original_record["function"],
                    verify_path, replay_timeout_seconds, run_dirs["json_dir"], "baseline",
                    entry_point=entry_point,
                )
            except ReplayTimeoutError:
                raise ValueError(
                    f"optimise() aborted: replaying the current unmodified code "
                    f"timed out after {replay_timeout_seconds:.1f}s - nothing was "
                    f"changed and no LLM calls were made"
                )
            except ReplayError as exc:
                raise ValueError(
                    f"optimise() aborted: replaying the current unmodified code "
                    f"failed - nothing was changed and no LLM calls were made: {exc}"
                )
            if len(baseline_invocations) != len(historical_calls) or any(
                not _outputs_match(hist["outputs"], inv.outputs)
                for inv, hist in zip(baseline_invocations, historical_calls)
            ):
                raise ValueError(
                    "optimise() aborted: replaying the current unmodified code did "
                    f"not reproduce all {len(historical_calls)} recorded call(s) - "
                    "nothing was changed and no LLM calls were made"
                )
            baseline_execution_time_ms = sum(inv.execution_time_ms for inv in baseline_invocations)
            baseline_memory_kb = _aggregate_peak_memory_kb(baseline_invocations)
            original_inputs_out = [c["inputs"] for c in historical_calls]
            original_outputs_out = [inv.outputs for inv in baseline_invocations]

        baseline_loc = self._adapter.prompt_fields(original_code)["original_loc"]
        baseline_values = {
            "speed": baseline_execution_time_ms,
            "loc": baseline_loc,
            "memory": baseline_memory_kb,
        }
        baseline_metric = baseline_values[objective]

        best = dict(original_snapshot)
        best["metric"] = baseline_metric
        best["version"] = None

        metric_label = {
            "loc": "non-blank lines of code",
            "memory": "peak memory (KB)",
        }.get(objective, "execution time (ms)")

        attempts = []
        transcript_entries = []
        history_entries = []

        for i in range(1, tries + 1):
            working_base = best if mode == "iterative" else original_snapshot
            storage.restore_state(json_path, working_base, func=func)

            feedback = None
            if mode == "iterative":
                feedback = f"Current best {metric_label}: {best['metric']}. Beat this."
                if history_entries:
                    feedback += "\n\n" + "\n\n".join(history_entries)

            attempt = {
                "try": i, "version": None, "code": None, "inputs": None,
                "outputs": None, "execution_time_ms": None, "loc_no_comments": None,
                "peak_memory_kb": None, "condition": "rejected", "error": None,
                "correct": None, "source": "agent" if propose else "llm",
                "code_sha256": None,
            }
            transcript_entry = {
                "try": i, "version": None, "sent_system": None, "sent_prompt": None,
                "sent_code": None, "received_code": None, "mapping": None,
            }

            try:
                if propose is not None:
                    # restore_state() above put working_base's code back on
                    # disk and in the record, so this is genuinely what the
                    # candidate has to improve on - the original in
                    # independent mode, the running best in iterative.
                    base_code = list(working_base["record"].get("code") or [])
                    lines = propose(base_code, feedback, {
                        "objective": objective,
                        "constraints": constraints,
                        "baseline": dict(baseline_values),
                        "best_metric": best["metric"],
                        "metric_label": metric_label,
                        "try": i,
                        "tries": tries,
                        "mode": mode,
                        "function": original_record.get("function"),
                    })
                    if not lines or not isinstance(lines, (list, tuple)):
                        raise ValueError(
                            f"propose() must return a non-empty list of code lines, "
                            f"got {type(lines).__name__}"
                        )
                    version = storage.write_agent_version(
                        json_path, working_base["record"], list(lines),
                    )
                else:
                    self.submit(func, feedback=feedback)
                    version = storage.latest_refactored_version(json_path)
            except Exception as exc:
                attempt["error"] = str(exc)
                attempts.append(attempt)
                transcript_entries.append(transcript_entry)
                if mode == "iterative":
                    history_entries.append(_describe_attempt(
                        attempt, constraints, baseline_values, {}, best["metric"],
                        metric_label, replay_timeout_seconds,
                    ))
                continue
            attempt["version"] = version
            transcript_entry["version"] = version
            (
                transcript_entry["sent_system"], transcript_entry["sent_prompt"],
                transcript_entry["sent_code"], transcript_entry["received_code"],
            ) = storage.read_refactored_version_detail(json_path, version)
            transcript_entry["mapping"] = storage.read_privacy_mapping(json_path, version)

            try:
                storage.apply_swap(
                    json_path, version, self._adapter,
                    backup_dir=run_dirs["backups_dir"], backup_content="post",
                )
            except ValueError as exc:
                attempt["error"] = str(exc)
                attempts.append(attempt)
                transcript_entries.append(transcript_entry)
                if mode == "iterative":
                    history_entries.append(_describe_attempt(
                        attempt, constraints, baseline_values, {}, best["metric"],
                        metric_label, replay_timeout_seconds,
                    ))
                continue

            with open(json_path, "r", encoding="utf-8") as fh:
                current_record = json.loads(fh.read())
            current_code = current_record.get("code") or []
            attempt["code"] = current_code
            # Hashed here, after apply_swap and before any replay: this is the
            # code that is actually on disk and about to be executed, which is
            # the only thing the measurements below can honestly attest to.
            # Hashing what propose() returned instead would attest to the
            # candidate, not to what ran.
            attempt["code_sha256"] = storage.sha256_of_lines(current_code)
            attempt["loc_no_comments"] = self._adapter.prompt_fields(current_code)["original_loc"]

            if use_captured_inputs:
                attempt["inputs"] = original_inputs_out
                try:
                    invocations = [
                        self._adapter.replay_call(
                            current_record["file"], current_record["function"],
                            call["inputs"], run_dirs["json_dir"], f"try{i}_{idx}",
                        )
                        for idx, call in enumerate(historical_calls)
                    ]
                except ReplayError as exc:
                    attempt["error"] = str(exc)
                    storage.restore_state(json_path, working_base, func=func)
                    attempts.append(attempt)
                    transcript_entries.append(transcript_entry)
                    if mode == "iterative":
                        history_entries.append(_describe_attempt(
                            attempt, constraints, baseline_values, {}, best["metric"],
                            metric_label, replay_timeout_seconds,
                        ))
                    continue

                attempt["outputs"] = [inv.outputs for inv in invocations]
                attempt["execution_time_ms"] = sum(inv.execution_time_ms for inv in invocations)
                attempt["peak_memory_kb"] = _aggregate_peak_memory_kb(invocations)
                correct = all(
                    _outputs_match(hist["outputs"], inv.outputs)
                    for inv, hist in zip(invocations, historical_calls)
                )
                attempt["correct"] = correct
            else:
                attempt["inputs"] = original_inputs_out
                verify_path = os.path.join(run_dirs["json_dir"], f"replay_try{i}_verify.json")
                try:
                    invocations = self._adapter.replay_entry(
                        current_record["file"], current_record["function"],
                        verify_path, replay_timeout_seconds, run_dirs["json_dir"], f"try{i}",
                        entry_point=entry_point,
                    )
                except ReplayTimeoutError as exc:
                    attempt["condition"] = "timeout"
                    attempt["error"] = str(exc)
                    storage.restore_state(json_path, working_base, func=func)
                    attempts.append(attempt)
                    transcript_entries.append(transcript_entry)
                    if mode == "iterative":
                        history_entries.append(_describe_attempt(
                            attempt, constraints, baseline_values, {}, best["metric"],
                            metric_label, replay_timeout_seconds,
                        ))
                    continue
                except ReplayError as exc:
                    attempt["error"] = str(exc)
                    storage.restore_state(json_path, working_base, func=func)
                    attempts.append(attempt)
                    transcript_entries.append(transcript_entry)
                    if mode == "iterative":
                        history_entries.append(_describe_attempt(
                            attempt, constraints, baseline_values, {}, best["metric"],
                            metric_label, replay_timeout_seconds,
                        ))
                    continue

                attempt["outputs"] = [inv.outputs for inv in invocations]
                attempt["execution_time_ms"] = sum(inv.execution_time_ms for inv in invocations)
                attempt["peak_memory_kb"] = _aggregate_peak_memory_kb(invocations)
                correct = (
                    len(invocations) == len(historical_calls)
                    and all(
                        _outputs_match(hist["outputs"], inv.outputs)
                        for inv, hist in zip(invocations, historical_calls)
                    )
                )
                attempt["correct"] = correct

            current_values = {
                "speed": attempt["execution_time_ms"],
                "loc": attempt["loc_no_comments"],
                "memory": attempt["peak_memory_kb"],
            }
            metric_value = current_values[objective]

            constraint_violated = correct and any(
                current_values[metric] > (
                    spec["max"] if "max" in spec else baseline_values[metric] * spec["max_relative"]
                )
                for metric, spec in constraints.items()
            )

            if correct and constraint_violated:
                attempt["condition"] = "constraint_violated"
                storage.restore_state(json_path, working_base, func=func)
                attempts.append(attempt)
                transcript_entries.append(transcript_entry)
                if mode == "iterative":
                    history_entries.append(_describe_attempt(
                        attempt, constraints, baseline_values, current_values,
                        best["metric"], metric_label, replay_timeout_seconds,
                    ))
            elif correct and metric_value < best["metric"]:
                attempt["condition"] = "accepted"
                best = storage.snapshot_state(json_path)
                best["metric"] = metric_value
                best["version"] = version
                attempts.append(attempt)
                transcript_entries.append(transcript_entry)
                if mode == "iterative":
                    history_entries.append(_describe_attempt(
                        attempt, constraints, baseline_values, current_values,
                        best["metric"], metric_label, replay_timeout_seconds,
                    ))
                if mode == "independent":
                    storage.restore_state(json_path, original_snapshot, func=func)
            else:
                storage.restore_state(json_path, working_base, func=func)
                attempts.append(attempt)
                transcript_entries.append(transcript_entry)
                if mode == "iterative":
                    history_entries.append(_describe_attempt(
                        attempt, constraints, baseline_values, current_values,
                        best["metric"], metric_label, replay_timeout_seconds,
                    ))

        # Unconditional - independent mode leaves disk at the pristine
        # original between tries, so the loop's own invariant doesn't
        # guarantee the winner is what's on disk when it ends.
        storage.restore_state(json_path, best, func=func)

        from . import llm
        provider = llm.get_active_provider()

        result = {
            "objective": objective,
            "constraints": constraints,
            "mode": mode,
            "use_captured_inputs": use_captured_inputs,
            "entry_point": entry_point,
            "provider": {"name": provider.name, "model": getattr(provider, "model", None)},
            "tries_requested": tries,
            "function": original_record.get("function"),
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "winner_version": best["version"],
            "winner_metric": best["metric"],
            "original_code": original_code,
            "original_inputs": original_inputs_out,
            "original": {
                "outputs": original_outputs_out,
                "execution_time_ms": baseline_execution_time_ms,
                "loc_no_comments": baseline_loc,
                "peak_memory_kb": baseline_memory_kb,
            },
            "attempts": attempts,
        }
        storage.write_optimise_history(json_path, result, output_dir=run_dirs["json_dir"])
        transcript_path = storage.write_llm_transcript(run_dirs["json_dir"], transcript_entries)
        storage.finalize_optimise_run_dir(json_path, run_dirs["json_dir"])

        from . import report
        report_path = report.generate_html(
            result, os.path.join(run_dirs["run_dir"], "report.html"),
            transcript_entries=transcript_entries,
        )

        result["run_dir"] = run_dirs["run_dir"]
        result["report_path"] = report_path
        result["llm_transcript_path"] = transcript_path
        return result
