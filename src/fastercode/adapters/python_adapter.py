import ast
import hashlib
import inspect
import json
import os
import runpy
import subprocess
import sys
import time
import traceback
import tracemalloc

from . import python_discovery, python_privacy
from .base import (
    AgentAdapter, AnonymizationResult, ExecutionContext, FunctionInfo,
    InvocationResult, LanguageAdapter, PromptSpec, ReplayError,
    ReplayTimeoutError, SourceLocation,
)
from .. import storage

_REPLAY_TIMEOUT_SECONDS = 120

_KEEP_REPR_MAXLEN = 2000


def _start_memory_trace():
    # Returns whether tracemalloc was already running before this call - a
    # tracked call nested inside another tracked call just samples the
    # existing (outer) trace's peak instead of starting a fresh one, since
    # starting/stopping here would reset the outer call's own measurement.
    # Known limitation: a nested call's own peak isn't isolated from its
    # caller's in that case.
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    return already_tracing


def _stop_memory_trace(already_tracing):
    _, peak_bytes = tracemalloc.get_traced_memory()
    if not already_tracing:
        tracemalloc.stop()
    return peak_bytes / 1024.0

_SYSTEM_LOC = (
    "You are a senior Python refactoring assistant. "
    "Given a single Python function, reduce non-comment, non-blank lines of code while "
    "preserving behavior. Keep the same function name and signature. Remove dead code, "
    "collapse redundant logic, use built-ins and comprehensions where appropriate. "
    "Return only code."
)

_SYSTEM_SPEED = (
    "You are a senior Python performance engineer. Your sole objective is to reduce "
    "a function's execution time while keeping all existing tests passing. "
    "Prefer algorithmic improvements over micro-optimisations. "
    "Keep the same function name and signature. Return only code. "
    "Do NOT add memoization, caching, or any persistent state (e.g. functools.lru_cache, "
    "custom dicts stored on the function, module-level caches) — improve the function's "
    "own computation, not how often it runs."
)

_PROMPT_LOC = (
    "Refactor the following Python function to reduce non-comment, non-blank lines of code "
    "while preserving behavior exactly. The original has {original_loc} non-comment, non-blank lines — "
    "your refactored version MUST have strictly fewer (less than {original_loc}). "
    "Returning the original or same number of lines is not acceptable. "
    "Keep the same function name and signature. "
    "Strategies: remove dead code, collapse nested loops into comprehensions or built-ins (sum, any, all, map, filter), "
    "eliminate redundant variables, use dict/set constructors instead of loops. "
    "Return ONLY JSON matching {{\"lines\": [list of strings]}}.\n\n{code}\n"
)

_PROMPT_SPEED = (
    "Refactor the following Python function to minimize execution time while preserving behavior.\n"
    "Keep the same function name and signature.\n\n"
    "Optimization strategies to consider:\n"
    "  - Prefer single-pass O(n) algorithms over multiple passes\n"
    "  - Replace Python loops with built-in C-implemented functions (sum, map, filter, any, all)\n"
    "  - Use list/dict/set comprehensions over explicit loops\n"
    "  - Pre-compute constants and attribute lookups outside loops\n"
    "  - Use numpy/scipy for numeric array operations if already imported\n\n"
    "Do NOT add memoization, caching, or persistent state of any kind.\n"
    "Return ONLY JSON matching {{\"lines\": [list of strings]}}.\n\n"
    "Function:\n{code}\n"
)

_SYSTEM_MEMORY = (
    "You are a senior Python performance engineer. Your sole objective is to reduce "
    "a function's peak memory usage while keeping all existing tests passing. "
    "Prefer algorithmic/data-structure changes that avoid materializing large "
    "intermediate collections over micro-optimisations. "
    "Keep the same function name and signature. Return only code. "
    "Do NOT add memoization, caching, or any persistent state (e.g. functools.lru_cache, "
    "custom dicts stored on the function, module-level caches) — trading memory for speed "
    "this way works against the objective, not for it."
)

_PROMPT_MEMORY = (
    "Refactor the following Python function to minimize its peak memory usage while "
    "preserving behavior.\n"
    "Keep the same function name and signature.\n\n"
    "Optimization strategies to consider:\n"
    "  - Prefer generators/iterators over materializing full lists when the whole "
    "collection isn't needed at once\n"
    "  - Avoid redundant copies of the same data (in-place operations where safe)\n"
    "  - Use built-in C-implemented functions (sum, map, filter, any, all) instead of "
    "building intermediate Python-level collections\n"
    "  - Stream/chunk large inputs instead of loading everything into memory at once\n\n"
    "Do NOT add memoization, caching, or persistent state of any kind - that increases "
    "memory retained across calls, which works against this objective.\n"
    "Return ONLY JSON matching {{\"lines\": [list of strings]}}.\n\n"
    "Function:\n{code}\n"
)


def _safe_repr(value):
    try:
        text = repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"
    if len(text) > _KEEP_REPR_MAXLEN:
        text = text[:_KEEP_REPR_MAXLEN] + "..."
    return text


def _split_outputs(result):
    if isinstance(result, tuple):
        return {f"return_{index}": value for index, value in enumerate(result)}
    return {"return": result}


class PythonAdapter(LanguageAdapter, AgentAdapter):

    # --- engine methods ---

    def get_source_location(self, target):
        try:
            source_lines, start_line = inspect.getsourcelines(target)
            end_line = start_line + len(source_lines) - 1

            def_index = 0
            for index, line in enumerate(source_lines):
                if line.lstrip().startswith(("def ", "async def ")):
                    def_index = index
                    break

            start_line += def_index
            code = [line.rstrip("\n") for line in source_lines[def_index:]]
        except (OSError, TypeError):
            start_line = target.__code__.co_firstlineno
            end_line = None
            code = None

        try:
            file = inspect.getsourcefile(target) or target.__code__.co_filename
        except TypeError:
            file = target.__code__.co_filename

        qualname_parts = target.__qualname__.split(".")
        if len(qualname_parts) >= 2 and qualname_parts[-2] != "<locals>":
            class_name = qualname_parts[-2]
        else:
            class_name = ""

        return SourceLocation(
            file=file,
            start_line=start_line,
            end_line=end_line,
            code=code,
            qualified_name=target.__qualname__,
            simple_name=target.__name__,
            class_name=class_name,
        )

    def content_hash(self, source_location):
        code = source_location.code
        hash_code = "\n".join(line for line in code if line.strip()) if code else ""
        return hashlib.sha256(
            "|".join([
                source_location.file, source_location.class_name,
                source_location.simple_name, hash_code,
            ]).encode("utf-8")
        ).hexdigest()

    def make_argument_binder(self, target):
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            signature = None

        if signature is None:
            def binder(args, kwargs):
                return {"args": list(args), "kwargs": dict(kwargs)}
            return binder

        def binder(args, kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)

        return binder

    def invoke_and_capture(self, target, args, kwargs, requested_variables):
        kept_variables = {}

        if requested_variables:
            previous_tracer = sys.gettrace()

            def _local_tracer(frame, event, arg):
                if event == "return":
                    for name in requested_variables:
                        if name in frame.f_locals:
                            kept_variables[name] = _safe_repr(frame.f_locals[name])
                        else:
                            kept_variables.setdefault(name, "<not found>")
                return _local_tracer

            def _global_tracer(frame, event, arg):
                if event == "call" and frame.f_code is target.__code__:
                    return _local_tracer
                return None

            sys.settrace(_global_tracer)
            already_tracing_memory = _start_memory_trace()
            try:
                call_start = time.perf_counter()
                result = target(*args, **kwargs)
                execution_time_ms = (time.perf_counter() - call_start) * 1000
            finally:
                peak_memory_kb = _stop_memory_trace(already_tracing_memory)
                sys.settrace(previous_tracer)

            for name in requested_variables:
                kept_variables.setdefault(name, "<not found>")
        else:
            already_tracing_memory = _start_memory_trace()
            try:
                call_start = time.perf_counter()
                result = target(*args, **kwargs)
                execution_time_ms = (time.perf_counter() - call_start) * 1000
            finally:
                peak_memory_kb = _stop_memory_trace(already_tracing_memory)

        outputs = _split_outputs(result)

        return InvocationResult(
            result=result,
            execution_time_ms=execution_time_ms,
            captured_variables=kept_variables,
            outputs=outputs,
            peak_memory_kb=peak_memory_kb,
        )

    def extract_kept_lines(self, source_location, requested_ranges):
        kept_lines = {}
        code = source_location.code
        start_line = source_location.start_line
        for range_start, range_end in requested_ranges:
            range_key = f"{range_start}-{range_end}"
            start_offset = range_start - start_line
            end_offset = range_end - start_line
            if code is not None and 0 <= start_offset and end_offset < len(code):
                kept_lines[range_key] = code[start_offset:end_offset + 1]
            else:
                kept_lines[range_key] = "<not found>"
        return kept_lines

    def update_keep_call_in_source(self, updated_lines, source_text, function_name,
                                    new_ranges, start_line, end_line, line_delta):
        # Rewrites the literal optimise.keep(func, lines=[(a, b), ...]) call in
        # the source so it reflects the post-swap line numbers - otherwise
        # re-running the script would re-register the old, now-stale numbers.
        # Best-effort: returns False (never raises) if the call can't be
        # confidently located/matched.
        try:
            tree = ast.parse(source_text)
        except SyntaxError:
            return False

        target_call = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "keep"):
                continue
            if not node.args:
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Name) and first_arg.id == function_name:
                target_call = node
                break

        if target_call is None:
            return False

        lines_kwarg = None
        for kw in target_call.keywords:
            if kw.arg == "lines":
                lines_kwarg = kw.value
                break

        if lines_kwarg is None or not isinstance(lines_kwarg, ast.List):
            return False

        tuple_nodes = lines_kwarg.elts
        if len(tuple_nodes) != len(new_ranges):
            return False

        edits = []
        for tuple_node, (new_start, new_end) in zip(tuple_nodes, new_ranges):
            if not isinstance(tuple_node, ast.Tuple) or len(tuple_node.elts) != 2:
                return False
            start_const, end_const = tuple_node.elts
            if not (isinstance(start_const, ast.Constant) and isinstance(end_const, ast.Constant)):
                return False
            edits.append((start_const, new_start))
            edits.append((end_const, new_end))

        # Apply from the bottom of the file up, right-to-left within a line, so
        # earlier edits' column offsets stay valid even if digit counts change.
        edits.sort(key=lambda e: (e[0].lineno, e[0].col_offset), reverse=True)

        for const_node, new_value in edits:
            orig_lineno = const_node.lineno
            if orig_lineno > end_line:
                new_lineno = orig_lineno + line_delta
            elif orig_lineno < start_line:
                new_lineno = orig_lineno
            else:
                # Shouldn't happen - the keep() call is a separate statement,
                # not inside the tracked function's own body.
                return False

            idx = new_lineno - 1
            if idx < 0 or idx >= len(updated_lines):
                return False

            line_text = updated_lines[idx]
            col_start = const_node.col_offset
            col_end = const_node.end_col_offset
            if col_end > len(line_text):
                return False
            updated_lines[idx] = line_text[:col_start] + str(new_value) + line_text[col_end:]

        return True

    def prompt_fields(self, code_lines):
        return {
            "code": "\n".join(code_lines),
            "original_loc": sum(1 for line in code_lines if line.strip()),
        }

    def get_prompts(self, objective):
        objective = (objective or "speed").lower()
        if objective == "loc":
            return PromptSpec(system=_SYSTEM_LOC, template=_PROMPT_LOC)
        if objective == "memory":
            return PromptSpec(system=_SYSTEM_MEMORY, template=_PROMPT_MEMORY)
        return PromptSpec(system=_SYSTEM_SPEED, template=_PROMPT_SPEED)

    def anonymize_record(self, record):
        keep_loc_ranges = [tuple(pair) for pair in (record.get("keep_spec") or {}).get("lines") or []]
        result = python_privacy.anonymize_source(
            record.get("code") or [],
            keep_loc_ranges=keep_loc_ranges,
            function_start_line=record.get("start_line", 1),
        )
        mapping = result["mapping"]
        real_to_pseudo = {real: pseudo for pseudo, real in mapping.items()}

        anonymized_keep_variables = {}
        for call_number, captured in (record.get("keep_variables") or {}).items():
            anonymized_keep_variables[call_number] = {
                real_to_pseudo.get(name, name): value for name, value in captured.items()
            }

        return AnonymizationResult(
            anonymized_code=result["anonymized_code"],
            anonymized_keep_variables=anonymized_keep_variables,
            anonymized_keep_loc=result["anonymized_keep_loc"],
            mapping=mapping,
        )

    def deanonymize_lines(self, lines, mapping):
        return python_privacy.deanonymize_source(lines, mapping)

    # --- agent-mode methods (AgentAdapter) ---

    def describe_function(self, script_path, function):
        found = python_discovery.describe_function(script_path, function)
        if found is None:
            return None
        source, parameters = found
        return FunctionInfo(source=source, parameters=parameters)

    def find_entry_points(self, script_path, function):
        return python_discovery.find_callers(script_path, function)

    def find_tests(self, script_path, function, limit=5):
        return python_discovery.find_tests_referencing(script_path, function, limit)

    def record_test_calls(self, script_path, function, test_id, wrapped):
        python_discovery.run_test(script_path, function, test_id, wrapped)

    def supported_strategies(self, function):
        # A method needs an entry point: captured-input replay reconstructs a
        # call in a fresh process from JSON-native inputs alone, and there is
        # no way to rebuild the instance `self` was bound to.
        if "." in function:
            return frozenset({"entry"})
        return frozenset({"entry", "test", "inputs"})

    def replay_call(self, script_path, target_name, inputs, work_dir, label):
        os.makedirs(work_dir, exist_ok=True)
        request_path = os.path.join(work_dir, f"replay_{label}.request.json")
        response_path = os.path.join(work_dir, f"replay_{label}.response.json")
        if os.path.exists(response_path):
            os.remove(response_path)

        with open(request_path, "w", encoding="utf-8") as fh:
            json.dump({
                "script_path": os.path.abspath(script_path),
                "target_name": target_name,
                "inputs": inputs,
            }, fh)

        cmd = [sys.executable, "-m", "fastercode.adapters.python_adapter", request_path, response_path]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=_REPLAY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise ReplayError(
                f"replay of '{target_name}' in {script_path} timed out after "
                f"{_REPLAY_TIMEOUT_SECONDS}s"
            )

        if not os.path.exists(response_path):
            raise ReplayError(f"replay subprocess for '{target_name}' produced no response")
        with open(response_path, "r", encoding="utf-8") as fh:
            response = json.loads(fh.read())

        if not response.get("ok"):
            raise ReplayError(response.get("error", "unknown replay failure"))
        if not response.get("outputs_json_native", False):
            raise ReplayError(
                f"'{target_name}' returned a value that isn't plain JSON-serializable - "
                f"cannot verify correctness against the recorded baseline"
            )

        outputs = response["outputs"]
        return InvocationResult(
            result=outputs.get("return"),
            execution_time_ms=response["execution_time_ms"],
            captured_variables={},
            outputs=outputs,
            peak_memory_kb=response.get("peak_memory_kb", 0.0),
        )

    def replay_entry(self, script_path, target_name, verify_json_path, timeout_seconds,
                      work_dir, label, entry_point=None):
        os.makedirs(work_dir, exist_ok=True)
        request_path = os.path.join(work_dir, f"replay_{label}.request.json")
        response_path = os.path.join(work_dir, f"replay_{label}.response.json")
        if os.path.exists(response_path):
            os.remove(response_path)

        with open(request_path, "w", encoding="utf-8") as fh:
            json.dump({
                "script_path": os.path.abspath(script_path),
                "target_name": target_name,
                "verify_json_path": verify_json_path,
                "entry_point": entry_point,
            }, fh)

        cmd = [
            sys.executable, "-m", "fastercode.adapters.python_adapter",
            request_path, response_path, "--entry",
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise ReplayTimeoutError(
                f"replay of '{target_name}' in {script_path} timed out after "
                f"{timeout_seconds:.1f}s"
            )

        if not os.path.exists(response_path):
            raise ReplayError(f"replay subprocess for '{target_name}' produced no response")
        with open(response_path, "r", encoding="utf-8") as fh:
            response = json.loads(fh.read())

        if not response.get("ok"):
            raise ReplayError(response.get("error", "unknown replay failure"))

        return [
            InvocationResult(
                result=call["outputs"].get("return"),
                execution_time_ms=call["execution_time_ms"],
                captured_variables={},
                outputs=call["outputs"],
                peak_memory_kb=call.get("peak_memory_kb", 0.0),
            )
            for call in response["calls"]
        ]

    # --- orchestration methods ---

    def load_script(self, script_path, entry_point):
        if not os.path.exists(script_path):
            raise FileNotFoundError(script_path)

        ns = runpy.run_path(script_path, run_name="fastercode_worker")

        if entry_point not in ns:
            raise LookupError(entry_point)

        # Critical: the live, mutable globals the script's own code actually
        # looks names up in - runpy.run_path()'s return value is only a
        # shallow copy and patching it has no effect on what the module's own
        # functions call.
        live_globals = ns[entry_point].__globals__

        return ExecutionContext(ns=ns, live_globals=live_globals, entry_point=entry_point)

    def resolve_target(self, live_globals, target_name):
        if "." in target_name:
            class_name, method_name = target_name.split(".", 1)
            cls = live_globals.get(class_name)
            if cls is None or not hasattr(cls, method_name):
                return None
            return getattr(cls, method_name)
        return live_globals.get(target_name)

    def patch_target(self, live_globals, target_name, wrapped):
        if "." in target_name:
            class_name, method_name = target_name.split(".", 1)
            cls = live_globals[class_name]
            setattr(cls, method_name, wrapped)
        else:
            live_globals[target_name] = wrapped

    def call_entry_point(self, live_globals, entry_point):
        live_globals[entry_point]()

    def run_isolated(self, config_path):
        # Spawns the orchestrator module as an isolated subprocess in worker
        # mode - isolation today, and the natural fit for eventually invoking
        # other languages' interpreters/binaries the same way.
        cmd = [sys.executable, "-m", "fastercode.orchestrator", config_path, "--worker"]
        result = subprocess.run(cmd)
        return result.returncode


def _run_replay_worker(request_path, response_path):
    # Runs inside the fresh subprocess spawned by replay_call() - re-imports
    # the target script from scratch (so it sees whatever apply_swap() just
    # wrote to disk) and calls the requested function directly. No
    # `.__globals__` trick needed here (unlike load_script): nothing gets
    # patched, so there's no "other caller" that needs to observe a mutation
    # - `ns[target_name]` is the real function object (same identity as in
    # the module's own globals; only the dict runpy.run_path() returns is a
    # shallow copy), and calling it directly resolves its own names correctly.
    try:
        with open(request_path, "r", encoding="utf-8") as fh:
            request = json.loads(fh.read())
        script_path = request["script_path"]
        target_name = request["target_name"]
        inputs = request["inputs"]

        if not os.path.exists(script_path):
            raise FileNotFoundError(script_path)

        adapter = PythonAdapter()
        ns = runpy.run_path(script_path, run_name="fastercode_replay")
        target = adapter.resolve_target(ns, target_name)
        if target is None:
            raise LookupError(f"target '{target_name}' not found in {script_path}")

        # If the target is itself @optimise.track-decorated in source (as
        # opposed to dynamically patched by the orchestrator), calling it
        # as-is would re-enter its own tracking wrapper on every replay -
        # adding that wrapper's own overhead to the timing measurement and
        # writing a fresh, ever-accumulating tracking.json for whatever
        # candidate code happens to be on disk at that moment. track()'s
        # wrapper is built with functools.wraps, which sets __wrapped__ to
        # the raw function - unwrap it so replay measures the function
        # itself, not the instrumentation around it.
        target = getattr(target, "__wrapped__", target)

        already_tracing_memory = _start_memory_trace()
        try:
            call_start = time.perf_counter()
            result = target(**inputs)
            execution_time_ms = (time.perf_counter() - call_start) * 1000
        finally:
            peak_memory_kb = _stop_memory_trace(already_tracing_memory)

        outputs = _split_outputs(result)
        try:
            json.dumps(outputs)
            outputs_json_native = True
        except TypeError:
            outputs_json_native = False

        response = {
            "ok": True,
            "execution_time_ms": execution_time_ms,
            "outputs": outputs,
            "outputs_json_native": outputs_json_native,
            "peak_memory_kb": peak_memory_kb,
        }
    except Exception:
        response = {"ok": False, "error": traceback.format_exc()}

    with open(response_path, "w", encoding="utf-8") as fh:
        json.dump(response, fh, cls=storage.RefactorJSONEncoder)


def _run_replay_entry_worker(request_path, response_path):
    # Runs inside the fresh subprocess spawned by replay_entry(). Unlike
    # _run_replay_worker (which reconstructs one call from stored inputs),
    # this lets the entry script's own real code drive whatever calls it
    # naturally makes, and reports all of them back - core.py compares the
    # full set against its own historical calls, rather than this worker
    # picking just one.
    verify_json_path = None
    try:
        with open(request_path, "r", encoding="utf-8") as fh:
            request = json.loads(fh.read())
        script_path = request["script_path"]
        target_name = request["target_name"]
        verify_json_path = request["verify_json_path"]
        entry_point = request.get("entry_point")

        if not os.path.exists(script_path):
            raise FileNotFoundError(script_path)

        # Every track()-recorded call made anywhere in this process (by
        # this target, or incidentally by another @track-decorated function
        # this script happens to import) is redirected here instead of the
        # real, persistent tracking.json - see core.py's track().
        #
        # The record at verify_json_path describes ONE function, so track()
        # also needs to know which one this replay is about: without it, a
        # script that tracks its own functions overwrites the target's
        # record and this worker reports the wrong function's calls.
        os.environ["FASTERCODE_REPLAY_JSON_PATH"] = verify_json_path
        os.environ["FASTERCODE_REPLAY_TARGET"] = target_name

        if entry_point is None:
            # Direct/native usage: @track already lives in script_path
            # itself. run_name="__main__" (unlike replay_call's
            # "fastercode_replay") makes if __name__ == "__main__": guarded
            # code fire too, matching a real `python script_path` run -
            # track()'s wrapper (redirected above) records whatever calls
            # the script's own code makes naturally.
            runpy.run_path(script_path, run_name="__main__")
        else:
            # job.yaml usage: target_name is only ever dynamically patched
            # onto an undecorated script, never decorated in source - a
            # bare script run would execute it completely unwrapped, so
            # mirror orchestrator.run_worker()'s own patch-then-call
            # sequence for this one target instead.
            from ..core import refactor

            adapter = PythonAdapter()
            context = adapter.load_script(script_path, entry_point)
            original = adapter.resolve_target(context.live_globals, target_name)
            if original is None:
                raise LookupError(f"target '{target_name}' not found in {script_path}")
            wrapped = refactor().track(original)
            adapter.patch_target(context.live_globals, target_name, wrapped)
            adapter.call_entry_point(context.live_globals, entry_point)

        if os.path.exists(verify_json_path):
            with open(verify_json_path, "r", encoding="utf-8") as fh:
                verify_record = json.loads(fh.read())
            calls = verify_record.get("calls") or {}
            ordered_calls = [calls[key] for key in sorted(calls, key=int)]
        else:
            # Tracked function was never actually called during this run.
            ordered_calls = []

        response = {"ok": True, "calls": ordered_calls}
    except Exception:
        response = {"ok": False, "error": traceback.format_exc()}
    finally:
        os.environ.pop("FASTERCODE_REPLAY_JSON_PATH", None)
        os.environ.pop("FASTERCODE_REPLAY_TARGET", None)

    with open(response_path, "w", encoding="utf-8") as fh:
        json.dump(response, fh, cls=storage.RefactorJSONEncoder)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[3] == "--entry":
        _run_replay_entry_worker(sys.argv[1], sys.argv[2])
    else:
        _run_replay_worker(sys.argv[1], sys.argv[2])
