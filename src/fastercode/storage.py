import ast
import datetime
import hashlib
import json
import os
import shutil
import sys

DATA_DIR = ".fastercode"


def resolve_data_dir(explicit=None):
    # Precedence: an explicit path (e.g. refactor(results_dir=...) or a
    # job.yaml's own results_dir:) wins outright; otherwise fall back to
    # FASTERCODE_RESULTS_DIR if set, then the hardcoded default - so
    # existing callers that pass/set nothing keep today's behavior exactly.
    if explicit:
        return explicit
    return os.environ.get("FASTERCODE_RESULTS_DIR", DATA_DIR)


class RefactorJSONEncoder(json.JSONEncoder):
    def default(self, o):
        try:
            return {"__type__": type(o).__name__, "__repr__": repr(o)}
        except Exception:
            return {"__type__": type(o).__name__, "__repr__": "<unrepresentable>"}


def find_sublist(haystack, needle):
    # Finds the starting index of an exact, contiguous match of `needle`
    # within `haystack`, or None if no such match exists.
    n = len(needle)
    if n == 0 or n > len(haystack):
        return None
    for i in range(len(haystack) - n + 1):
        if haystack[i:i + n] == needle:
            return i
    return None


def strip_blank_edges(lines):
    # Strips leading/trailing blank/whitespace-only lines. Blank lines are
    # incidental spacing, not meaningful code - requiring them to match
    # verbatim made the keep_loc check too fragile (e.g. the LLM inserting an
    # unrelated line next to a boundary blank line would otherwise break an
    # exact match even though the actual code was preserved untouched).
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def remap_keep_loc(record, refactored_lines, start_line):
    # Validates that every keep_spec line range is still present verbatim in
    # the refactored code, and computes its new absolute position. Raises
    # ValueError (aborting the swap - nothing gets written) if any requested
    # range can't be verified, since keep_loc is meant to guarantee those
    # lines survive the refactor unchanged.
    keep_spec = record.get("keep_spec")
    if not keep_spec or not keep_spec.get("lines"):
        return None, None

    keep_loc = record.get("keep_LOC") or {}
    updated_keep_loc = {}
    updated_line_ranges = []

    for pair in keep_spec["lines"]:
        range_key = f"{pair[0]}-{pair[1]}"
        old_text_lines = keep_loc.get(range_key)
        if not old_text_lines or old_text_lines == "<not found>":
            raise ValueError(
                f"cannot verify keep_loc range {range_key}: no prior captured "
                f"content to match against - call the tracked function at least "
                f"once after keep() before swapping"
            )

        trimmed_lines = strip_blank_edges(old_text_lines)
        if not trimmed_lines:
            raise ValueError(
                f"keep_loc range {range_key} is blank lines only - nothing to verify"
            )

        match_index = find_sublist(refactored_lines, trimmed_lines)
        if match_index is None:
            raise ValueError(
                f"refactored version does not preserve the keep_loc lines "
                f"{range_key} verbatim - swap aborted, nothing was changed"
            )

        new_start = start_line + match_index
        new_end = new_start + len(trimmed_lines) - 1
        updated_keep_loc[f"{new_start}-{new_end}"] = trimmed_lines
        updated_line_ranges.append([new_start, new_end])

    return updated_keep_loc, updated_line_ranges


def find_function_node(tree, function_name):
    # Locates 'name' or 'Class.method' in a parsed module. Lives here rather
    # than in _agent.py because find_tracking_json() below needs it too, and
    # storage is the lower layer - _agent already imports this module.
    target = function_name.split(".")[-1]
    class_name = function_name.split(".")[0] if "." in function_name else None

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != target:
            continue
        if class_name is None:
            return node
        for parent in ast.walk(tree):
            if isinstance(parent, ast.ClassDef) and parent.name == class_name:
                if node in parent.body:
                    return node
    return None


def function_source_lines(file_path, function_name):
    # The function's source as it stands in the file RIGHT NOW, in the same
    # shape get_source_location() records into tracking.json: from the `def`
    # line (ast.lineno skips decorators on 3.8+) through the end of the body.
    # Returns None if the file is unreadable, unparseable, or has no such
    # function - all of which just mean "can't compare", not "error".
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return None

    node = find_function_node(tree, function_name)
    if node is None:
        return None
    return source.splitlines()[node.lineno - 1:node.end_lineno]


def _significant(lines):
    # Blank lines are not part of a function's identity - content_hash()
    # drops them too, so comparisons here must agree with the hashing that
    # decided the directory name in the first place.
    return [line for line in (lines or []) if line.strip()]


def find_tracking_jsons(file_path, function_name, json_dir=None):
    # Every tracking.json recording this file+function, best match first.
    #
    # There is usually more than one: the directory name is the hash of the
    # function's SOURCE, so each version optimise() has swapped in leaves its
    # own folder behind. Returning whichever os.listdir() happened to yield
    # first meant callers silently read a stale run's calls.
    #
    # Order: the record whose code matches what is in the file right now,
    # then the rest newest-first. The source check has to come first because
    # mtime is wrong in exactly the case that matters - swap_version() back to
    # an older measured version makes the CORRECT directory the older one.
    json_dir = resolve_data_dir(json_dir)
    target_file = os.path.abspath(file_path)
    if not os.path.isdir(json_dir):
        return []

    current = _significant(function_source_lines(target_file, function_name))

    matches = []
    for hash_name in sorted(os.listdir(json_dir)):
        candidate = os.path.join(json_dir, hash_name, "tracking.json")
        if not os.path.exists(candidate):
            continue
        with open(candidate, "r", encoding="utf-8") as fh:
            try:
                data = json.loads(fh.read())
            except json.JSONDecodeError:
                continue
        if (
            os.path.abspath(data.get("file", "")) == target_file
            and data.get("function") == function_name
        ):
            is_current = bool(current) and _significant(data.get("code")) == current
            try:
                mtime = os.path.getmtime(candidate)
            except OSError:
                mtime = 0.0
            matches.append((not is_current, -mtime, candidate))

    return [candidate for _, _, candidate in sorted(matches)]


def find_tracking_json(file_path, function_name, json_dir=None):
    # The single best match - see find_tracking_jsons() for the ordering.
    # Callers that need to know the choice was ambiguous can use the plural.
    matches = find_tracking_jsons(file_path, function_name, json_dir=json_dir)
    return matches[0] if matches else None


def record_function_call(json_path, meta, call_data,
                          kept_lines=None, kept_variables=None, keep_spec=None):
    os.makedirs(os.path.dirname(json_path), exist_ok=True)

    keep_spec_json = None
    if keep_spec:
        keep_spec_json = {
            "variables": keep_spec["variables"],
            "lines": [list(pair) for pair in keep_spec["lines"]],
        }

    existing = None
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        if raw:
            existing = json.loads(raw)
            if not isinstance(existing, dict):
                existing = None

    if existing is not None:
        next_call_number = str(len(existing["calls"]) + 1)
        existing.update(meta)
        if keep_spec_json:
            existing["keep_spec"] = keep_spec_json
        if kept_lines:
            existing["keep_LOC"] = kept_lines
        if kept_variables:
            existing.setdefault("keep_variables", {})[next_call_number] = kept_variables
        existing["calls"][next_call_number] = call_data
        record = existing
    else:
        record = dict(meta)
        if keep_spec_json:
            record["keep_spec"] = keep_spec_json
        if kept_lines:
            record["keep_LOC"] = kept_lines
        if kept_variables:
            record["keep_variables"] = {"1": kept_variables}
        record["calls"] = {"1": call_data}

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, cls=RefactorJSONEncoder, indent=2)


def reset_calls(json_path):
    # Drops the recorded calls from an existing tracking.json while keeping
    # the file/function metadata, so the next run starts from an empty call
    # history instead of appending to the previous one.
    #
    # Why this is needed: record_function_call() above appends, and the
    # tracking directory is keyed by the function's source hash - so running
    # the same recording twice leaves two calls behind for identical code.
    # optimise()'s baseline check then re-runs the entry script once, sees one
    # call where the record claims two, and aborts. Harmless for a human who
    # runs a script once; fatal for an agent invoked repeatedly.
    #
    # Returns True if a record was found and cleared, False if there was
    # nothing to clear.
    if not os.path.exists(json_path):
        return False

    with open(json_path, "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    if not raw:
        return False

    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(record, dict):
        return False

    record["calls"] = {}
    record.pop("keep_variables", None)

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, cls=RefactorJSONEncoder, indent=2)
    return True


def apply_swap(json_path, version, adapter, backup_dir=None, backup_content="pre"):
    # Core swap logic, independent of any live decorated function object -
    # shared by refactor.swap_code() and the CLI, which resolves json_path
    # by scanning .fastercode/*/tracking.json instead. `backup_dir` lets
    # optimise() redirect its own interim-try backups into a per-run
    # folder instead of the hash-dir root; every other caller omits it and
    # gets today's behavior unchanged. `backup_content` selects what that
    # backup file actually holds: "pre" (default) is the safety-net
    # semantics swap_code()/the CLI rely on (a copy of what's about to be
    # overwritten, so you can restore it by hand if you don't like the
    # result); optimise() uses "post" instead, since it wants a record of
    # the candidate that was actually tested that try, regardless of
    # accept/reject - "what this swap overwrote" is nearly always the
    # untouched original in optimise()'s independent mode (every rejected
    # try gets restored before the next one), which is uninteresting on
    # its own compared to "what did this specific try's code look like".
    if not os.path.exists(json_path):
        raise ValueError(
            f"no tracked calls recorded yet for this function (expected {json_path})"
        )

    with open(json_path, "r", encoding="utf-8") as fh:
        record = json.loads(fh.read())

    hash_dir = os.path.dirname(json_path)
    refactored_path = os.path.join(hash_dir, "refactored.json")
    if not os.path.exists(refactored_path):
        raise ValueError(
            f"no refactored code found yet (expected {refactored_path}); call submit() first"
        )

    with open(refactored_path, "r", encoding="utf-8") as fh:
        refactored_data = json.loads(fh.read())

    version_key = str(version)
    versions = refactored_data.get("versions", {})
    if version_key not in versions:
        raise ValueError(
            f"no refactored version '{version}' found (available: {sorted(versions.keys())})"
        )
    refactored_lines = versions[version_key]["code"]

    source_file = record["file"]
    start_line = record["start_line"]
    end_line = record["end_line"]

    # Validate/remap keep_loc BEFORE touching any file - if this raises, the
    # swap is aborted with nothing backed up or modified.
    updated_keep_loc, updated_line_ranges = remap_keep_loc(
        record, refactored_lines, start_line
    )

    with open(source_file, "r", encoding="utf-8") as fh:
        original_lines = fh.read().splitlines(keepends=True)

    new_lines = [line + "\n" for line in refactored_lines]
    updated_lines = original_lines[:start_line - 1] + new_lines + original_lines[end_line:]

    if updated_line_ranges is not None:
        line_delta = len(new_lines) - (end_line - start_line + 1)
        function_name = record.get("function", "").rsplit(".", 1)[-1]
        rewritten = adapter.update_keep_call_in_source(
            updated_lines, "".join(original_lines), function_name,
            updated_line_ranges, start_line, end_line, line_delta,
        )
        if not rewritten:
            print(
                f"[fastercode] warning: could not automatically update the "
                f"optimise.keep({function_name}, ...) call's line numbers in "
                f"{source_file} - update them by hand to "
                f"lines={[tuple(pair) for pair in updated_line_ranges]}",
                file=sys.stderr,
            )

    if backup_dir is None:
        backup_dir = hash_dir
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if backup_content == "post":
        backup_path = os.path.join(backup_dir, f"tested_{version}_{timestamp}.py")
        backup_lines = updated_lines
    else:
        backup_path = os.path.join(backup_dir, f"backup_{version}_{timestamp}.py")
        backup_lines = original_lines
    with open(backup_path, "w", encoding="utf-8") as fh:
        fh.writelines(backup_lines)
        fh.flush()
        os.fsync(fh.fileno())

    with open(source_file, "w", encoding="utf-8") as fh:
        fh.writelines(updated_lines)
        fh.flush()
        os.fsync(fh.fileno())

    # Always refresh start_line/end_line/code to match what was just
    # written - otherwise a second swap_code() call (without an intervening
    # tracked call to refresh these via record_function_call) would use
    # stale boundaries and corrupt the file by cutting the wrong line range.
    record["end_line"] = start_line + len(new_lines) - 1
    record["code"] = list(refactored_lines)
    if updated_line_ranges is not None:
        record["keep_spec"]["lines"] = updated_line_ranges
        record["keep_LOC"] = updated_keep_loc
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)

    return backup_path


def sha256_of_lines(lines):
    # Provenance hash for a candidate. Joined with "\n" so it matches how
    # apply_swap() writes the lines into the source file - hashing the list's
    # repr, or the file including its unrelated surrounding code, would both
    # attest to the wrong thing.
    text = "\n".join(lines or [])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_agent_version(json_path, record, lines, source="agent"):
    # Registers externally-authored code (an IDE agent's hot swap) as a
    # refactored.json version, so the rest of optimise() - apply_swap, replay,
    # comparison, scoring - can consume it exactly as it consumes an
    # LLM-authored one. Deliberately mirrors llm.process()'s record shape.
    #
    # sent_system/sent_prompt are None because no prompt existed: nothing was
    # sent anywhere. `source` is what lets a reader tell an agent's candidate
    # from a model's after the fact.
    hash_dir = os.path.dirname(json_path)
    os.makedirs(hash_dir, exist_ok=True)
    refactored_path = os.path.join(hash_dir, "refactored.json")

    if os.path.exists(refactored_path):
        with open(refactored_path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        data = json.loads(raw) if raw else {}
    else:
        data = {}

    data.setdefault("source_hash", os.path.basename(hash_dir))
    data.setdefault("source_json", json_path)
    data.setdefault("function", record.get("function"))
    versions = data.setdefault("versions", {})

    next_version = str(len(versions) + 1)
    versions[next_version] = {
        "code": list(lines),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sent_system": None,
        "sent_prompt": None,
        "sent_code": list(record.get("code") or []),
        "received_code": list(lines),
        "source": source,
    }

    with open(refactored_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

    return next_version


def latest_refactored_version(json_path):
    hash_dir = os.path.dirname(json_path)
    with open(os.path.join(hash_dir, "refactored.json"), "r", encoding="utf-8") as fh:
        versions = json.load(fh)["versions"]
    return max(versions, key=int)


def snapshot_state(json_path):
    # Captures enough to fully restore later: swap_code()/apply_swap()
    # mutates both the source file and tracking.json - a "best" or
    # "original" state for optimise()'s restore-between-tries logic needs
    # both, not just one.
    with open(json_path, "r", encoding="utf-8") as fh:
        record = json.loads(fh.read())
    with open(record["file"], "r", encoding="utf-8") as fh:
        source_text = fh.read()
    return {"record": record, "source_text": source_text}


def restore_state(json_path, snapshot, func=None):
    record = snapshot["record"]
    with open(record["file"], "w", encoding="utf-8") as fh:
        fh.write(snapshot["source_text"])
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    if func is not None:
        keep_spec = record.get("keep_spec")
        if keep_spec:
            func.refactor_keep_spec = {
                "variables": keep_spec.get("variables", []),
                "lines": [tuple(pair) for pair in keep_spec.get("lines", [])],
            }


def write_optimise_history(json_path, data, output_dir=None):
    if output_dir is None:
        output_dir = os.path.dirname(json_path)
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "optimise.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, cls=RefactorJSONEncoder, indent=2)
    return path


def write_llm_transcript(json_dir, entries):
    # A run's own record of exactly what was sent/received per try -
    # separate from optimise.json on purpose: this is a network/privacy
    # audit concern (true regardless of whether a try ever got far enough
    # to be tested), not a testing/benchmarking one.
    os.makedirs(json_dir, exist_ok=True)
    path = os.path.join(json_dir, "llm_transcript.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"attempts": entries}, fh, cls=RefactorJSONEncoder, indent=2)
    return path


def make_optimise_run_dir(json_path):
    # One self-contained, timestamped folder per optimise() run: backups/
    # for the interim swaps that try made, json/ for optimise.json plus the
    # replay request/response files and end-of-run ledger snapshots.
    #
    # The suffix matters, not just for tidiness: the timestamp only has
    # second resolution, and two runs landing in one folder don't merely mix
    # their artifacts - the replay verification files are APPEND-based, so
    # the second run's baseline replay reads the first run's calls too and
    # concludes the recorded history reproduced when it didn't. A run must
    # get its own directory or its measurements are not its own.
    hash_dir = os.path.dirname(json_path)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs_root = os.path.join(hash_dir, "optimise_runs")
    run_dir = os.path.join(runs_root, f"run_{timestamp}")

    suffix = 1
    while os.path.exists(run_dir):
        suffix += 1
        run_dir = os.path.join(runs_root, f"run_{timestamp}_{suffix}")

    backups_dir = os.path.join(run_dir, "backups")
    json_dir = os.path.join(run_dir, "json")
    os.makedirs(backups_dir)
    os.makedirs(json_dir)
    return {"run_dir": run_dir, "backups_dir": backups_dir, "json_dir": json_dir}


def finalize_optimise_run_dir(json_path, json_dir):
    # Copies tracking.json (the real-call history) as it stood at the end
    # of the run into the run's own json/ folder. refactored.json is
    # deliberately NOT copied here - optimise.json already carries
    # everything from it that's relevant to this run (code/sent_code/
    # received_code/mapping per try), so a copy would just be redundant.
    shutil.copyfile(json_path, os.path.join(json_dir, "tracking.json"))


def contains_encoder_placeholder(value):
    # Detects RefactorJSONEncoder's own fallback shape
    # ({"__type__": ..., "__repr__": ...}), which json.dumps() alone can't
    # catch: the placeholder is itself plain-JSON-native, but it means the
    # real original value (e.g. a class instance) was never actually
    # JSON-serializable and can't be reconstructed for a subprocess replay.
    if isinstance(value, dict):
        if set(value.keys()) == {"__type__", "__repr__"}:
            return True
        return any(contains_encoder_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_encoder_placeholder(v) for v in value)
    return False


PRIVACY_SUBDIR = "privacy"


def write_privacy_mapping(json_path, version, mapping):
    # .fastercode/<hash>/privacy/mapping_<version>.json - an audit-trail copy.
    # submit() itself threads the mapping through in-memory (its own `anon`
    # object), never round-tripping through disk; optimise() does read this
    # back (via read_privacy_mapping) to embed it in that try's own record
    # in optimise.json.
    hash_dir = os.path.dirname(json_path)
    privacy_dir = os.path.join(hash_dir, PRIVACY_SUBDIR)
    os.makedirs(privacy_dir, exist_ok=True)
    path = os.path.join(privacy_dir, f"mapping_{version}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2)
    return path


def overwrite_refactored_version_code(json_path, version, new_lines):
    # Rewrites versions[str(version)]["code"] in refactored.json in place -
    # the seam that lets llm.py stay completely unaware of privacy_mode:
    # it writes the (pseudonymized) code as usual, and this call is what
    # replaces it with the de-anonymized real code immediately afterward.
    hash_dir = os.path.dirname(json_path)
    refactored_path = os.path.join(hash_dir, "refactored.json")
    with open(refactored_path, "r", encoding="utf-8") as fh:
        data = json.loads(fh.read())
    data["versions"][str(version)]["code"] = list(new_lines)
    with open(refactored_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def read_refactored_version_detail(json_path, version):
    # (sent_system, sent_prompt, sent_code, received_code) for one version -
    # used by optimise() to build its own llm_transcript.json entry for a
    # try, without needing to keep a copy of refactored.json around.
    hash_dir = os.path.dirname(json_path)
    with open(os.path.join(hash_dir, "refactored.json"), "r", encoding="utf-8") as fh:
        data = json.loads(fh.read())
    entry = data["versions"][str(version)]
    return (
        entry.get("sent_system"), entry.get("sent_prompt"),
        entry.get("sent_code"), entry.get("received_code"),
    )


def read_privacy_mapping(json_path, version):
    hash_dir = os.path.dirname(json_path)
    mapping_path = os.path.join(hash_dir, PRIVACY_SUBDIR, f"mapping_{version}.json")
    if not os.path.exists(mapping_path):
        return None
    with open(mapping_path, "r", encoding="utf-8") as fh:
        return json.loads(fh.read())
