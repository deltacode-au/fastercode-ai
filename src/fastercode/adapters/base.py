from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SourceLocation:
    file: str
    start_line: int
    end_line: int | None
    code: list[str] | None
    qualified_name: str
    simple_name: str
    class_name: str


@dataclass
class FunctionInfo:
    # What agent mode can learn about a function from the FILE alone, before
    # anything is loaded - distinct from SourceLocation, which describes a
    # live target object the caller already holds.
    source: list[str]
    parameters: list[str]


@dataclass
class InvocationResult:
    result: object
    execution_time_ms: float
    captured_variables: dict
    outputs: dict
    peak_memory_kb: float = 0.0


@dataclass
class PromptSpec:
    system: str
    template: str


@dataclass
class ExecutionContext:
    ns: dict
    live_globals: dict
    entry_point: str


class ReplayError(RuntimeError):
    """Raised by replay_call()/replay_entry() when a fresh-process
    re-execution can't be used to judge a candidate - subprocess failure,
    an unresolved target, the target itself raising, or (replay_call only)
    a non-JSON-serializable result."""


class ReplayTimeoutError(ReplayError):
    """Raised by replay_entry() specifically when the replay subprocess
    exceeds its timeout budget - kept distinct from other ReplayErrors so
    optimise() can record a "timeout" condition rather than a generic
    "rejected" one."""


@dataclass
class AnonymizationResult:
    anonymized_code: list[str]
    anonymized_keep_variables: dict
    anonymized_keep_loc: dict
    mapping: dict  # pseudonym -> real name


class PrivacyError(RuntimeError):
    """Raised by anonymize_record() for v1 out-of-scope constructs (nested
    def/async def, lambda, in-function import) or a keep_loc range that
    doesn't align to a whole statement / contiguous statement run."""


class LanguageAdapter(ABC):
    """
    The seam between refactor's language-agnostic orchestration (core.py,
    storage.py, llm.py, orchestrator.py) and one target language's own
    introspection/execution mechanisms.
    """

    # --- engine methods: used by track()/keep()/submit()/swap_code() ---

    @abstractmethod
    def get_source_location(self, target) -> SourceLocation:
        """Best-effort source info for `target`. Must never raise."""
        raise NotImplementedError

    @abstractmethod
    def content_hash(self, source_location: SourceLocation) -> str:
        """Full hex digest identifying this function's identity + content."""
        raise NotImplementedError

    @abstractmethod
    def make_argument_binder(self, target):
        """Returns a callable(args, kwargs) -> dict, computed once per decoration."""
        raise NotImplementedError

    @abstractmethod
    def invoke_and_capture(self, target, args, kwargs, requested_variables) -> InvocationResult:
        """
        Calls `target`, times it, and captures any locals named in
        `requested_variables`. Exceptions from `target` propagate unmodified.
        """
        raise NotImplementedError

    @abstractmethod
    def extract_kept_lines(self, source_location: SourceLocation, requested_ranges) -> dict:
        """Re-extracts requested (start, end) line ranges fresh on every call."""
        raise NotImplementedError

    @abstractmethod
    def update_keep_call_in_source(self, updated_lines, source_text, function_name,
                                    new_ranges, start_line, end_line, line_delta) -> bool:
        """
        Best-effort rewrite of a keep() registration call's line numbers in
        source, after a swap shifts them. Must never raise; False means
        "couldn't confidently do it" and only suppresses this convenience
        rewrite, never an already-verified swap.
        """
        raise NotImplementedError

    @abstractmethod
    def prompt_fields(self, code_lines) -> dict:
        """The dict used to .format() this adapter's prompt templates."""
        raise NotImplementedError

    @abstractmethod
    def get_prompts(self, objective: str) -> PromptSpec:
        """The (system, template) pair for a given objective (e.g. "speed"/"loc")."""
        raise NotImplementedError

    @abstractmethod
    def replay_call(self, script_path: str, target_name: str, inputs: dict,
                     work_dir: str, label: str) -> InvocationResult:
        """
        Backs optimise(): spawns a fresh, isolated process that re-imports
        `script_path` from scratch (seeing whatever is currently on disk -
        e.g. right after apply_swap), resolves `target_name` via this
        adapter's own resolve_target() rules, and calls it with `inputs`
        (must be plain JSON-native - there is no way to reconstruct
        arbitrary objects, e.g. a bound method's `self`, in a fresh
        process). Splits the return value into outputs the same way
        invoke_and_capture does (return_0/return_1/... for a tuple,
        "return" otherwise), so callers can directly compare against a
        recorded call's "outputs".

        `work_dir` is a scratch directory (the caller's hash_dir) for
        request/response files; `label` only affects their names.

        Raises ReplayError if the subprocess fails/times out, the target
        can't be resolved, it raises, or its return value isn't plain
        JSON-serializable (checked in the child, not the parent).
        """
        raise NotImplementedError

    @abstractmethod
    def replay_entry(self, script_path: str, target_name: str, verify_json_path: str,
                      timeout_seconds: float, work_dir: str, label: str,
                      entry_point: str | None = None) -> list[InvocationResult]:
        """
        Backs optimise()'s default correctness check (used unless
        use_captured_inputs=True): re-runs the real entry script fresh in a
        subprocess and returns an InvocationResult for every call
        `target_name` makes during that run, in order - the caller
        compares these against its own historical calls, rather than this
        method reconstructing a single call from stored inputs.

        If `entry_point` is None (direct/native usage - the target's own
        @track decorator already lives in script_path), the subprocess
        re-executes script_path as if run directly, so
        `if __name__ == "__main__":` guards fire (matching a real
        `python script_path` invocation) - unlike replay_call(), which uses
        a run_name that deliberately does not trigger them.

        If `entry_point` is given (job.yaml usage - target_name is only
        ever dynamically patched onto an undecorated script, never
        decorated in source), the subprocess instead loads script_path,
        resolves target_name, wraps it with track(), patches it in, and
        calls entry_point() - mirroring orchestrator.run_worker()'s own
        patching sequence for this one target, since a bare script run
        would otherwise execute target_name completely unwrapped.

        Either way, every call the tracked function makes during the run
        must be redirected into a fresh, empty file at `verify_json_path`
        (e.g. via the FASTERCODE_REPLAY_JSON_PATH env var core.py's track()
        checks) rather than the real, persistent tracking.json.
        `work_dir`/`label` only affect scratch request/response file names.

        Raises ReplayError if the subprocess fails or the target can't be
        resolved; raises ReplayTimeoutError specifically if the subprocess
        exceeds `timeout_seconds`, so callers can record a distinct
        "timeout" condition rather than a generic rejection.
        """
        raise NotImplementedError

    @abstractmethod
    def anonymize_record(self, record: dict) -> AnonymizationResult:
        """
        Backs submit(privacy_mode=True): produces a sanitized version of
        `record["code"]` (identifiers renamed to pseudonyms, comments/
        docstrings stripped) plus the pseudonymized equivalents of
        `record["keep_variables"]`/`record["keep_LOC"]` (so a protected
        name/line is never described to the LLM using its real name) and
        the pseudonym->real mapping needed to reverse it all locally.
        Raises PrivacyError for constructs this adapter can't safely
        anonymize (e.g. a language-specific out-of-scope construct) -
        never silently mis-anonymizes.
        """
        raise NotImplementedError

    @abstractmethod
    def deanonymize_lines(self, lines: list, mapping: dict) -> list:
        """Reverses anonymize_record()'s renaming in the LLM's returned code."""
        raise NotImplementedError

    # --- orchestration methods: used by orchestrator.py's dynamic patching ---

    @abstractmethod
    def load_script(self, script_path: str, entry_point: str) -> ExecutionContext:
        """
        Loads the target script. `context.live_globals` MUST be the live,
        mutable namespace the script's own code actually looks names up in
        at call time. Raises FileNotFoundError/LookupError for a missing
        script/entry point.
        """
        raise NotImplementedError

    @abstractmethod
    def resolve_target(self, live_globals: dict, target_name: str):
        """Resolves a dotted `Class.method` or plain name. None if not found."""
        raise NotImplementedError

    @abstractmethod
    def patch_target(self, live_globals: dict, target_name: str, wrapped) -> None:
        """Writes `wrapped` back in place of the original target."""
        raise NotImplementedError

    @abstractmethod
    def call_entry_point(self, live_globals: dict, entry_point: str) -> None:
        """Calls the entry point function."""
        raise NotImplementedError

    @abstractmethod
    def run_isolated(self, config_path: str) -> int:
        """Spawns an isolated process running this job in worker mode."""
        raise NotImplementedError


class AgentAdapter(ABC):
    """
    What agent mode needs from a language, on top of LanguageAdapter.

    Deliberately a separate ABC rather than more methods on LanguageAdapter:
    core.py drives a language entirely through LanguageAdapter, so an adapter
    can support decorator/job.yaml use without supporting agent mode at all.
    agent.analyse() checks for this one and reports "agent mode isn't
    supported for <language>" rather than failing somewhere deeper.

    Everything here works from a FILE PATH and must not execute the script -
    unlike LanguageAdapter.get_source_location(), which needs a live target
    the caller already resolved.

    The division of labour: an adapter returns FACTS about the language, and
    agent.analyse() decides how to order, rank and describe them. That keeps
    the wording an agent reads identical across languages.
    """

    @abstractmethod
    def describe_function(self, script_path: str, function: str) -> FunctionInfo | None:
        """
        The function's current source lines and parameter names, read from
        the file. Returns None if `script_path` has no such function - which
        includes the file being unparseable, since both mean the same thing
        to the caller.
        """
        raise NotImplementedError

    @abstractmethod
    def find_entry_points(self, script_path: str, function: str) -> list[str]:
        """
        Names of other functions in the same file that call `function`, in
        source order. Each is usable as an entry point for an end-to-end
        replay, which is the strongest evidence available.
        """
        raise NotImplementedError

    @abstractmethod
    def find_tests(self, script_path: str, function: str, limit: int = 5) -> list[str]:
        """
        Ids of tests that plausibly exercise `function`, in whatever form
        record_test_calls() accepts for this language.

        Prefer false negatives. A test that turns out not to call the target
        records no calls and fails loudly, which costs less than handing the
        user a list of junk options to choose between.
        """
        raise NotImplementedError

    @abstractmethod
    def record_test_calls(self, script_path: str, function: str, test_id: str,
                          wrapped) -> None:
        """
        Runs `test_id` with `wrapped` patched in place of `function`, so the
        calls that test makes get recorded.

        Raises ValueError if the test does not pass - calls made by a failing
        test are not evidence of anything. Must restore whatever it patched,
        pass or fail.
        """
        raise NotImplementedError

    @abstractmethod
    def supported_strategies(self, function: str) -> frozenset:
        """
        Which of 'entry'/'test'/'inputs' this language can honestly measure
        for THIS target.

        Takes the qualified name because the answer can depend on it: a
        captured-input replay that cannot reconstruct instance state supports
        'inputs' for a plain function but not for a method. A language with no
        in-process invocation omits 'inputs' entirely; one with no test-runner
        integration omits 'test'. This is where a language's verification
        nuances belong - analyse() only offers what this returns.
        """
        raise NotImplementedError
