"""
Tests for the machinery behind the MCP server.

`agent` is internal - the supported way to reach it is fastercode.mcp_server
(see test_mcp_server.py, which drives the tools a client actually calls).
These tests cover the layer underneath: that the recording strategies work,
that a wrong candidate is caught, that a run's provenance is recorded
honestly, and that repeated runs don't trip over each other.

They do not assert that any particular candidate wins: on a small function the
timing differences are inside the measurement noise, which is a real property
of the tool rather than something a test should pin down.
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastercode import llm, storage
from fastercode._agent import agent
from fastercode.adapters import adapter_for_file, python_discovery
from fastercode.adapters.base import AgentAdapter, FunctionInfo, LanguageAdapter
from fastercode.adapters.python_adapter import PythonAdapter

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FIXTURE = os.path.join(_FIXTURES, "sample_target.py")
NAN_FIXTURE = os.path.join(_FIXTURES, "nan_target.py")
MULTI_TRACK_FIXTURE = os.path.join(_FIXTURES, "multi_track_target.py")

FASTER = [
    "def calculate_average(values):",
    "    try:",
    "        n = len(values)",
    "    except TypeError:",
    "        values = list(values)",
    "        n = len(values)",
    "    return sum(values) / n",
]

WRONG = [
    "def calculate_average(values):",
    "    return 999.0",
]

BROKEN = [
    "def calculate_average(values):",
    "    raise RuntimeError('boom')",
]

# Accumulates with += rather than sum(): CPython 3.12+ sums floats with
# Neumaier compensation, which is MORE accurate than the fixture's naive
# left-to-right addition and so produces different bits. sum() here would be
# rejected for real - and the NaN tests would fail for the wrong reason.
NAN_EQUIVALENT = [
    "def moving_average(values, window):",
    "    nan = float('nan')",
    "    averages = [nan] * min(window - 1, len(values))",
    "",
    "    for index in range(window - 1, len(values)):",
    "        total = 0.0",
    "        for value in values[index - window + 1:index + 1]:",
    "            total += value",
    "        averages.append(total / window)",
    "",
    "    return averages",
]

NAN_WRONG = [
    "def moving_average(values, window):",
    "    return [0.0] * len(values)",
]

# The NaN positions line up with the original's, only the real numbers differ -
# so a comparison that treated NaN as equal to anything would accept this.
NAN_EVERYWHERE = [
    "def moving_average(values, window):",
    "    return [float('nan')] * len(values)",
]

MULTI_TRACK_EQUIVALENT = [
    "def calculate_average(values):",
    "    total = 0.0",
    "    for value in values:",
    "        total = total + value",
    "    return total / len(values)",
]


@pytest.fixture
def script(tmp_path):
    target = tmp_path / "sample_target.py"
    shutil.copy(FIXTURE, target)
    return str(target)


@pytest.fixture
def nan_script(tmp_path):
    target = tmp_path / "nan_target.py"
    shutil.copy(NAN_FIXTURE, target)
    return str(target)


@pytest.fixture
def multi_track_script(tmp_path, monkeypatch):
    # The fixture builds its own refactor(), which resolves a data dir at
    # construction time - point it at tmp_path so recording the script's own
    # tracked function doesn't drop a .fastercode/ into the repo.
    monkeypatch.setenv("FASTERCODE_RESULTS_DIR", str(tmp_path / "script_store"))
    target = tmp_path / "multi_track_target.py"
    shutil.copy(MULTI_TRACK_FIXTURE, target)
    return str(target)


@pytest.fixture
def results(tmp_path):
    return str(tmp_path / "results")


def _run(script, results, **kwargs):
    kwargs.setdefault("recording", "entry")
    if kwargs["recording"] == "entry":
        kwargs.setdefault("entry_point", "run_pipeline")
    return agent(objective="speed", results_dir=results).optimise_function(
        script, "calculate_average", **kwargs
    )


# ------------------------------------------------------------ storage layer

def test_reset_calls_clears_calls_but_keeps_metadata(script, results):
    _run(script, results, candidates=[FASTER])
    json_path = storage.find_tracking_json(script, "calculate_average", json_dir=results)

    with open(json_path, encoding="utf-8") as fh:
        before = json.load(fh)
    assert before["calls"]

    assert storage.reset_calls(json_path) is True

    with open(json_path, encoding="utf-8") as fh:
        after = json.load(fh)
    assert after["calls"] == {}
    assert after["function"] == before["function"]
    assert after["file"] == before["file"]
    assert after["code"] == before["code"]


def test_reset_calls_on_missing_file_is_a_no_op(tmp_path):
    assert storage.reset_calls(str(tmp_path / "nope.json")) is False


# --------------------------------------------------------------- provenance

def test_agent_run_records_the_agent_provider(script, results):
    result = _run(script, results, candidates=[FASTER])
    assert result["status"] == "ok"
    # The whole point: no LLM was called, so the record must not name one.
    assert result["provider"] == {"name": "agent", "model": None}


def test_active_provider_is_restored_for_human_callers(script, results):
    before = llm.get_active_provider()
    _run(script, results, candidates=[FASTER])
    assert llm.get_active_provider() is before
    assert llm.get_active_provider().name != "agent"


def test_agent_provider_refuses_to_be_called():
    from fastercode.providers.agent_provider import AgentProvider

    with pytest.raises(RuntimeError):
        AgentProvider().call("system", "prompt")
    with pytest.raises(RuntimeError):
        AgentProvider().set_credentials("key")


def test_agent_provider_is_not_selectable_from_a_job_yaml():
    from fastercode import providers

    assert "agent" not in providers.PROVIDERS


def test_agent_is_not_a_public_import():
    # Agent mode is reached through the MCP server, whose tool descriptions
    # carry the workflow contract. A bare `from fastercode import agent`
    # bypasses all of that, so it must not be an available path.
    #
    # Checked in a clean interpreter on purpose: this test module imports the
    # machinery itself, and importing a submodule binds it as an attribute of
    # the package, so in-process the name could appear to exist.
    import subprocess

    probe = subprocess.run(
        [sys.executable, "-c", "from fastercode import agent"],
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert probe.returncode != 0
    assert "ImportError" in probe.stderr or "cannot import name" in probe.stderr

    import fastercode

    assert fastercode.__all__ == ["refactor"]


# ---------------------------------------------------------------- recording

def test_entry_strategy_records_the_scripts_own_call(script, results):
    result = _run(script, results, candidates=[FASTER])
    assert result["summary"]["recorded_calls"] == 1
    assert result["summary"]["recording"] == "entry"
    assert result["use_captured_inputs"] is False


def test_inputs_strategy_records_every_supplied_call(script, results):
    result = _run(
        script, results, candidates=[FASTER], recording="inputs",
        inputs=[{"values": [1, 2, 3]}, {"values": [10, 20]}, [[4, 5, 6]]],
    )
    assert result["status"] == "ok"
    assert result["summary"]["recorded_calls"] == 3
    assert result["use_captured_inputs"] is True


def test_test_strategy_records_the_calls_the_test_makes(script, results, tmp_path):
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        "from sample_target import calculate_average\n"
        "\n"
        "def test_average():\n"
        "    assert calculate_average([1, 2, 3, 4, 5]) == 3.0\n"
        "    assert calculate_average([10, 20]) == 15.0\n",
        encoding="utf-8",
    )
    result = _run(
        script, results, candidates=[FASTER], recording="test",
        test=f"{test_file}::test_average",
    )
    assert result["status"] == "ok"
    assert result["summary"]["recorded_calls"] == 2
    assert result["use_captured_inputs"] is True


def test_a_failing_test_is_not_used_as_evidence(script, results, tmp_path):
    test_file = tmp_path / "test_broken.py"
    test_file.write_text(
        "from sample_target import calculate_average\n"
        "\n"
        "def test_average():\n"
        "    assert calculate_average([1, 2, 3]) == 999\n",
        encoding="utf-8",
    )
    result = _run(
        script, results, candidates=[FASTER], recording="test",
        test=f"{test_file}::test_average",
    )
    assert result["status"] == "error"
    assert result["stage"] == "recording"


def test_inputs_rejects_a_malformed_call_spec(script, results):
    result = _run(script, results, candidates=[FASTER], recording="inputs", inputs=[42])
    assert result["status"] == "error"
    assert result["stage"] == "recording"


# --------------------------------------------------------------- judgement

def test_a_wrong_candidate_is_rejected(script, results):
    result = _run(script, results, candidates=[WRONG])
    attempt = result["summary"]["attempts"][0]
    assert attempt["correct"] is False
    assert attempt["condition"] == "rejected"
    assert result["winner_version"] is None


def test_a_crashing_candidate_is_rejected_without_aborting_the_run(script, results):
    result = _run(script, results, candidates=[BROKEN, FASTER])
    assert result["status"] == "ok"
    assert result["summary"]["attempts"][0]["condition"] in ("rejected", "timeout")
    assert len(result["summary"]["attempts"]) == 2


def test_every_attempt_is_hashed_to_the_code_that_ran(script, results):
    result = _run(script, results, candidates=[FASTER, WRONG])
    hashes = [a["code_sha256"] for a in result["summary"]["attempts"]]
    assert all(hashes)
    assert hashes[0] != hashes[1]


def test_the_original_is_restored_when_nothing_wins(script, results):
    with open(script, encoding="utf-8") as fh:
        before = fh.read()
    result = _run(script, results, candidates=[WRONG])
    assert result["winner_version"] is None
    with open(script, encoding="utf-8") as fh:
        assert fh.read() == before


# ------------------------------------------------------------- regressions

def _run_nan(script, results, candidates, **kwargs):
    kwargs.setdefault("recording", "entry")
    if kwargs["recording"] == "entry":
        kwargs.setdefault("entry_point", "run_pipeline")
    return agent(objective="speed", results_dir=results).optimise_function(
        script, "moving_average", candidates=candidates, **kwargs
    )


@pytest.mark.parametrize("recording", ["entry", "inputs"])
def test_a_nan_output_reproduces_and_an_equivalent_candidate_is_accepted(
    nan_script, results, recording
):
    # Characterisation, not a regression: a function padding its output with
    # NaN has to be optimisable at all. This passes with or without
    # _outputs_match - plain == already agreed, because both sides are parsed
    # by json.loads and every NaN token decodes to json.decoder's single NaN
    # object, which list equality matches on identity. The test pins the
    # behaviour so it survives that detail changing. entry and inputs reach
    # different comparison sites, hence both.
    kwargs = {"recording": recording}
    if recording == "inputs":
        kwargs["inputs"] = [{"values": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], "window": 3}]

    result = _run_nan(nan_script, results, [NAN_EQUIVALENT], **kwargs)

    assert result["status"] == "ok"
    assert result["summary"]["attempts"][0]["correct"] is True


@pytest.mark.parametrize("candidate", [NAN_WRONG, NAN_EVERYWHERE],
                         ids=["constant", "nan-everywhere"])
def test_a_wrong_candidate_is_still_rejected_when_the_output_holds_nan(
    nan_script, results, candidate
):
    # The half that actually constrains: positional NaN counts as reproduced,
    # but nothing else loosens. "nan-everywhere" is the one that would slip
    # through a comparison treating NaN as equal to any value.
    result = _run_nan(nan_script, results, [candidate])

    assert result["status"] == "ok"
    assert result["summary"]["attempts"][0]["correct"] is False
    assert result["winner_version"] is None


def test_another_tracked_function_does_not_clobber_the_targets_record(
    multi_track_script, results
):
    # A replay redirects every tracked function in the subprocess into one
    # file holding ONE function's record, so the script's own summarise() used
    # to overwrite the target's - and the harness compared summarise's dict
    # against calculate_average's float.
    result = agent(objective="speed", results_dir=results).optimise_function(
        multi_track_script, "calculate_average", candidates=[MULTI_TRACK_EQUIVALENT],
        recording="entry", entry_point="run_pipeline",
    )

    assert result["status"] == "ok"
    assert result["summary"]["recorded_calls"] == 1

    # Not just "it didn't abort": the measurement has to be the target's.
    read_back = agent(results_dir=results).read_run(result["summary"]["run_dir"])
    assert read_back["optimise"]["original"]["outputs"] == [{"return": 3.0}]


def test_a_replay_does_not_leak_into_another_functions_real_store(
    multi_track_script, results, tmp_path
):
    # Scoping the recording to the target must not cost the redirect its
    # original job: replays still have to stay out of the persistent store.
    # One entry recording writes summarise's single call; the baseline and
    # candidate replays that follow must add nothing.
    agent(objective="speed", results_dir=results).optimise_function(
        multi_track_script, "calculate_average", candidates=[MULTI_TRACK_EQUIVALENT],
        recording="entry", entry_point="run_pipeline",
    )

    store = str(tmp_path / "script_store")
    tracking = storage.find_tracking_json(multi_track_script, "summarise", json_dir=store)
    assert tracking, "the script's own tracked function should have recorded once"
    with open(tracking, encoding="utf-8") as fh:
        assert len(json.load(fh)["calls"]) == 1


# ----------------------------------------------------------- repeatability

def test_a_second_run_against_the_same_store_does_not_abort(script, results):
    first = _run(script, results, candidates=[FASTER])
    assert first["status"] == "ok"

    # Same store, same code, same function - this is the case that used to
    # abort with "did not reproduce all N recorded call(s)" because the
    # recorded calls accumulated across runs.
    second = _run(script, results, candidates=[FASTER])
    assert second["status"] == "ok"
    assert second["summary"]["recorded_calls"] == 1


def test_back_to_back_runs_get_their_own_run_directory(script, results):
    # The run folder name is a UTC timestamp with second resolution, and runs
    # now finish well inside one second. Sharing a folder is not just untidy:
    # the replay verification files are append-based, so a second run's
    # baseline would read the first run's calls and conclude the recorded
    # history reproduced when it hadn't.
    run_dirs = [
        _run(script, results, candidates=[WRONG])["summary"]["run_dir"]
        for _ in range(3)
    ]
    assert len(set(run_dirs)) == 3
    for run_dir in run_dirs:
        assert os.path.exists(os.path.join(run_dir, "json", "optimise.json"))


def test_fresh_false_preserves_the_previous_call_history(script, results):
    # WRONG rather than FASTER on purpose: the tracking store is keyed by the
    # function's source hash, so a run that applies a winner rewrites the file
    # and the next run lands in a different store - which would hide the
    # accumulation this test is about. A rejected candidate leaves the source
    # untouched, so both runs share one store.
    _run(script, results, candidates=[WRONG])
    json_path = storage.find_tracking_json(script, "calculate_average", json_dir=results)
    result = _run(script, results, candidates=[WRONG], fresh=False)
    # Two runs' calls now coexist; optimise() correctly refuses to measure
    # against a history the entry script no longer reproduces.
    assert result["status"] == "error"
    assert result["stage"] == "optimise"
    assert result["tracking_json"] == json_path
    assert any("fresh=True" in step for step in result["recovery"])


# ------------------------------------------------------------------ output

def test_a_report_is_written_for_every_run(script, results):
    result = _run(script, results, candidates=[FASTER])
    report = result["summary"]["report_path"]
    assert report and os.path.exists(report)
    assert os.path.getsize(report) > 0


def test_runs_and_read_run_find_the_measurements(script, results):
    result = _run(script, results, candidates=[FASTER])
    a = agent(results_dir=results)

    listed = a.runs(script=script, function="calculate_average")
    assert listed and listed[0]["report_path"]

    read_back = a.read_run(result["summary"]["run_dir"])
    assert read_back["status"] == "ok"
    assert read_back["optimise"]["provider"]["name"] == "agent"


# ----------------------------------------------------------------- analyse

def test_analyse_finds_the_entry_point_and_falls_back_to_inputs(script, results):
    info = agent(results_dir=results).analyse(script, "calculate_average")
    assert info["status"] == "ok"
    assert info["parameters"] == ["values"]

    by_kind = {s["recording"]: s for s in info["strategies"]}
    assert by_kind["entry"]["entry_point"] == "run_pipeline"
    assert "inputs" in by_kind
    # Preference order matters: the caller shows the user the first one.
    assert info["strategies"][0]["recording"] == "entry"


def test_analyse_ignores_tests_that_only_mention_the_name(script, results, tmp_path):
    # A test that names the function but never reaches this script must not be
    # offered as a strategy - otherwise the caller puts junk options to its user.
    (tmp_path / "test_unrelated.py").write_text(
        "def test_something():\n"
        "    label = 'calculate_average'\n"
        "    assert label\n",
        encoding="utf-8",
    )
    # ...while one that actually imports the module is offered.
    (tmp_path / "test_real.py").write_text(
        "from sample_target import calculate_average\n"
        "\n"
        "def test_average():\n"
        "    assert calculate_average([1, 2]) == 1.5\n",
        encoding="utf-8",
    )
    info = agent(results_dir=results).analyse(script, "calculate_average")
    offered = [s["test"] for s in info["strategies"] if s["recording"] == "test"]
    assert any("test_real.py" in node for node in offered)
    assert not any("test_unrelated.py" in node for node in offered)


def test_analyse_does_not_offer_the_target_script_as_its_own_test(script, results):
    info = agent(results_dir=results).analyse(script, "calculate_average")
    offered = [s.get("test") or "" for s in info["strategies"]]
    assert not any(os.path.basename(script) in node for node in offered)


def test_analyse_reports_a_missing_function(script, results):
    info = agent(results_dir=results).analyse(script, "not_a_function")
    assert info["status"] == "error"


# ------------------------------------------------------------- arguments

def test_candidates_and_propose_are_mutually_exclusive(script, results):
    with pytest.raises(ValueError):
        _run(script, results, candidates=[FASTER], propose=lambda *a: FASTER)


def test_supplying_neither_candidates_nor_propose_is_an_error(script, results):
    with pytest.raises(ValueError):
        _run(script, results)


def test_entry_recording_requires_an_entry_point(script, results):
    with pytest.raises(ValueError):
        agent(results_dir=results).optimise_function(
            script, "calculate_average", candidates=[FASTER], recording="entry",
        )


def test_propose_is_still_accepted_directly(script, results):
    calls = []

    def propose(code, feedback, context):
        calls.append(context["try"])
        return FASTER

    result = _run(script, results, propose=propose, tries=2)
    assert result["status"] == "ok"
    assert calls == [1, 2]


# ------------------------------------------- state leaking between runs
#
# These all fail only when one interpreter does several recordings against
# files that change in between - i.e. the MCP server, and nothing else.

def test_an_edited_test_file_is_re_read_by_a_second_recording(script, results, tmp_path):
    # pytest caches test modules in sys.modules. Without evicting them, the
    # second run silently re-executes the FIRST version of the file and
    # reports a confident measurement of a workload that is no longer there.
    test_file = tmp_path / "test_edited.py"
    header = "from sample_target import calculate_average\n\ndef test_average():\n"

    test_file.write_text(header + "    assert calculate_average([1, 2, 3]) == 2.0\n",
                         encoding="utf-8")
    first = _run(script, results, candidates=[FASTER], recording="test",
                 test=f"{test_file}::test_average")
    assert first["summary"]["recorded_calls"] == 1

    test_file.write_text(
        header
        + "    assert calculate_average([1, 2, 3]) == 2.0\n"
        + "    assert calculate_average([10, 20]) == 15.0\n"
        + "    assert calculate_average([4, 5, 6]) == 5.0\n",
        encoding="utf-8",
    )
    second = _run(script, results, candidates=[FASTER], recording="test",
                  test=f"{test_file}::test_average")
    assert second["summary"]["recorded_calls"] == 3


def test_a_recorded_method_is_unpatched_afterwards(results, tmp_path):
    # The class keeps the tracked wrapper otherwise, so every later call in
    # the process records into a finished run's tracking.json.
    #
    # Driven through _record_test directly because optimise_function cannot
    # reach it: _prepare() passes entry_point=None for recording='test', and
    # _globals_anchor() rejects a dotted name without one.
    script = tmp_path / "method_target.py"
    script.write_text(
        "class Scaler:\n"
        "    def scale(self, values, factor):\n"
        "        return [value * factor for value in values]\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_method.py"
    test_file.write_text(
        "import method_target\n"
        "\n"
        "def test_scale():\n"
        "    assert method_target.Scaler().scale([1, 2], 2) == [2, 4]\n",
        encoding="utf-8",
    )

    instance = agent(results_dir=results)
    module = python_discovery.import_as_module(str(script))
    wrapped = instance.track(module.Scaler.scale)

    instance._record_test(str(script), "Scaler.scale",
                          f"{test_file}::test_scale", wrapped)

    # _record_test reloads the script, so the class object is legitimately a
    # new one - what matters is that it is not carrying the tracked wrapper.
    scaler = sys.modules["method_target"].Scaler
    assert not hasattr(scaler.scale, "refactor_json_path")

    # The consequence that actually bites: a later call must not append to a
    # finished run's call history.
    with open(wrapped.refactor_json_path, encoding="utf-8") as fh:
        before = json.load(fh)["calls"]
    scaler().scale([1, 2], 2)
    with open(wrapped.refactor_json_path, encoding="utf-8") as fh:
        assert json.load(fh)["calls"] == before


def test_two_scripts_with_the_same_basename_do_not_collide(results, tmp_path):
    # sys.modules is keyed by basename, so a second project's target.py would
    # reload the first project's file.
    first = tmp_path / "first" / "shared_name.py"
    second = tmp_path / "second" / "shared_name.py"
    for path, marker in ((first, "FIRST"), (second, "SECOND")):
        path.parent.mkdir(parents=True)
        path.write_text(f"MARKER = {marker!r}\n", encoding="utf-8")

    assert python_discovery.import_as_module(str(first)).MARKER == "FIRST"
    assert python_discovery.import_as_module(str(second)).MARKER == "SECOND"
    assert python_discovery.import_as_module(str(first)).MARKER == "FIRST"


# ------------------------------------------------------ language agnosticism

class _ToyAdapter(PythonAdapter, AgentAdapter):
    """
    A pretend language whose files are '<name> takes <params>' lines, sharing
    PythonAdapter's engine half only so this stays about discovery.

    The point of the ABC is that agent.analyse() can drive a language it has
    never heard of. If any of this needed an edit inside _agent.py, the seam
    would not be real.
    """

    def describe_function(self, script_path, function):
        for line in open(script_path, encoding="utf-8").read().splitlines():
            if line.startswith(f"{function} takes "):
                params = line.split(" takes ", 1)[1].split()
                return FunctionInfo(source=[line], parameters=params)
        return None

    def find_entry_points(self, script_path, function):
        return ["main"] if f"main calls {function}" in \
            open(script_path, encoding="utf-8").read() else []

    def find_tests(self, script_path, function, limit=5):
        return []          # this toy language has no test runner

    def record_test_calls(self, script_path, function, test_id, wrapped):
        raise AssertionError("find_tests() returns nothing, so this is unreachable")

    def supported_strategies(self, function):
        return frozenset({"entry"})    # no in-process invocation, so no 'inputs'


@pytest.fixture
def toy_script(tmp_path):
    path = tmp_path / "toy.made_up"
    path.write_text("scale takes values factor\nmain calls scale\n", encoding="utf-8")
    return str(path)


def test_analyse_drives_a_language_it_has_never_seen(toy_script):
    info = agent(adapter=_ToyAdapter()).analyse(toy_script, "scale")

    assert info["status"] == "ok"
    assert info["parameters"] == ["values", "factor"]
    assert info["source"] == ["scale takes values factor"]
    assert [s["recording"] for s in info["strategies"]] == ["entry"]
    assert info["strategies"][0]["entry_point"] == "main"


def test_a_language_that_cannot_do_inputs_is_never_offered_it(tmp_path):
    # The adapter says 'entry' only, and there is no caller to find - so the
    # answer is the "you need an entry point" strategy, never a fabricated
    # 'inputs' option the language could not actually honour.
    path = tmp_path / "lonely.made_up"
    path.write_text("scale takes values\n", encoding="utf-8")

    info = agent(adapter=_ToyAdapter()).analyse(str(path), "scale")

    assert [s["recording"] for s in info["strategies"]] == ["entry"]
    assert info["strategies"][0]["confidence"] == "required"
    assert info["strategies"][0]["entry_point"] is None


class _CoreOnlyAdapter(LanguageAdapter):
    """
    A LanguageAdapter with no AgentAdapter half - the shape a language takes
    when it supports decorator/job.yaml runs but not agent mode yet.
    """


# Cleared after class creation (ABCMeta recomputes it during), so the class can
# be instantiated without 17 stub methods. analyse() returns on the isinstance
# check before calling anything on the adapter, so there is nothing to stub.
_CoreOnlyAdapter.__abstractmethods__ = frozenset()


def test_analyse_says_so_when_the_adapter_has_no_agent_support(script, results):
    info = agent(adapter=_CoreOnlyAdapter(),
                 results_dir=results).analyse(script, "calculate_average")

    assert info["status"] == "error"
    assert "AgentAdapter" in info["reason"]
    assert "_CoreOnlyAdapter" in info["reason"]


def test_python_adapter_implements_both_halves():
    adapter = PythonAdapter()
    assert isinstance(adapter, LanguageAdapter)
    assert isinstance(adapter, AgentAdapter)


def test_a_method_cannot_be_measured_from_captured_inputs():
    # Python's own nuance, now owned by the adapter rather than analyse().
    adapter = PythonAdapter()
    assert adapter.supported_strategies("Scaler.scale") == frozenset({"entry"})
    assert "inputs" in adapter.supported_strategies("scale")


def test_adapter_is_chosen_by_extension_with_an_explicit_override(tmp_path):
    assert isinstance(adapter_for_file("a/b/thing.py"), PythonAdapter)
    assert isinstance(adapter_for_file("a/b/thing.weird", "python"), PythonAdapter)

    with pytest.raises(ValueError) as unknown_ext:
        adapter_for_file("a/b/thing.weird")
    assert ".py" in str(unknown_ext.value)

    with pytest.raises(ValueError) as unknown_lang:
        adapter_for_file("a/b/thing.py", "cobol")
    assert "cobol" in str(unknown_lang.value)


# ------------------------------------------------- locating a tracking.json

def _fake_tracking(results_dir, folder, script, function, code, mtime):
    path = os.path.join(results_dir, folder, "tracking.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "file": str(script), "function": function, "code": code,
            "calls": {"1": {"inputs": {"x": 1}, "outputs": {"return": 2}}},
        }, fh)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def lookup_target(tmp_path):
    path = tmp_path / "lookup_target.py"
    path.write_text("def compute(x):\n    return x + 1\n", encoding="utf-8")
    return path


CURRENT_CODE = ["def compute(x):", "    return x + 1"]
STALE_CODE = ["def compute(x):", "    return x + 2"]


def test_find_tracking_json_prefers_the_version_in_the_file(lookup_target, results):
    # Deliberately stacked against the answer: the stale record sorts first in
    # os.listdir AND is the more recent. Matching the source has to win both.
    stale = _fake_tracking(results, "aaa", lookup_target, "compute", STALE_CODE, 9000)
    current = _fake_tracking(results, "zzz", lookup_target, "compute", CURRENT_CODE, 1000)

    found = storage.find_tracking_json(str(lookup_target), "compute", json_dir=results)
    assert found == current
    assert found != stale


def test_find_tracking_json_falls_back_to_the_newest(lookup_target, results):
    # Neither matches the file, so recency is all that is left - but the older
    # one still sorts first in os.listdir.
    older = _fake_tracking(results, "aaa", lookup_target, "compute",
                           ["def compute(x):", "    return x + 7"], 1000)
    newer = _fake_tracking(results, "zzz", lookup_target, "compute", STALE_CODE, 9000)

    assert storage.find_tracking_json(str(lookup_target), "compute",
                                      json_dir=results) == newer
    assert storage.find_tracking_jsons(str(lookup_target), "compute",
                                       json_dir=results) == [newer, older]


def test_find_tracking_json_ignores_other_functions_and_files(lookup_target, results):
    other_file = lookup_target.parent / "another_target.py"
    _fake_tracking(results, "aaa", lookup_target, "something_else", CURRENT_CODE, 9000)
    _fake_tracking(results, "bbb", other_file, "compute", CURRENT_CODE, 9000)

    assert storage.find_tracking_json(str(lookup_target), "compute",
                                      json_dir=results) is None
