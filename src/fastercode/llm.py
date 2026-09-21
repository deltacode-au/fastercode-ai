import datetime
import json
import os

from . import providers

# The active provider used by process() below. Defaults to OpenAI so
# existing native usage (never calling set_credentials()) keeps working
# unchanged; set_credentials() swaps this for a different registered
# provider when a job.yaml (or direct caller) names one. Same kind of
# module-level runtime state this module already relied on before providers
# existed (it used to mutate OPENAI_API_KEY directly the same way).
_active_provider = providers.get_provider("openai")


def get_active_provider():
    """Whichever LLMProvider process() currently calls - see set_credentials()."""
    return _active_provider


def set_active_provider(provider_obj):
    """
    Swaps the active provider directly, bypassing the credential/model
    resolution set_credentials() does. Returns the provider it replaced, so a
    caller can restore it.

    This is for providers that have no credentials to apply - specifically
    agent.py's AgentProvider, which stands in for "no LLM was called at all"
    so optimise()'s record and report name the real source of the code.
    """
    global _active_provider
    previous = _active_provider
    _active_provider = provider_obj
    return previous


def _resolve_key_value(key):
    # ${ENV_VAR} indirection - the YAML never has to hold the real secret.
    # A plain literal string is used as-is (supported, but the indirection
    # is the recommended path).
    if isinstance(key, str) and key.startswith("${") and key.endswith("}"):
        env_name = key[2:-1]
        value = os.environ.get(env_name)
        if value is None:
            raise ValueError(
                f"api.key references environment variable '{env_name}', "
                f"which is not set"
            )
        return value
    return key


def set_credentials(provider, key, model=None):
    global _active_provider
    provider = (provider or "").lower()
    try:
        provider_obj = providers.get_provider(provider)
    except ValueError:
        raise ValueError(
            f"unsupported api provider '{provider}' - supported: "
            f"{sorted(providers.PROVIDERS)}"
        )
    provider_obj.set_credentials(_resolve_key_value(key))
    if model:
        provider_obj.set_model(model)
    _active_provider = provider_obj


def _build_keep_sections(record):
    sections = []

    keep_variables = record.get("keep_variables")
    if keep_variables:
        any_call = next(iter(keep_variables.values()), {})
        variable_names = sorted(any_call.keys())
        if variable_names:
            sections.append(
                "keep_variables:\n"
                "The following variables must retain their meaning/role and "
                "should not be removed or renamed: " + ", ".join(variable_names)
            )

    keep_loc = record.get("keep_LOC")
    if keep_loc:
        blocks = []
        for range_key, content in keep_loc.items():
            if content == "<not found>":
                continue
            block_text = "\n".join(content) if isinstance(content, list) else str(content)
            blocks.append(f"Lines {range_key}:\n{block_text}")
        if blocks:
            sections.append(
                "keep_loc:\n"
                "The following lines of code must remain constrained in the solution."
                "They must be a core part of the refactored code:\n\n" + "\n\n".join(blocks)
            )

    return sections


def _strip_trailing_blank(lines):
    out = list(lines)
    while out and out[-1].strip() == "":
        out.pop()
    return out


def _build_feedback_section(feedback):
    if not feedback:
        return None
    return "optimise() feedback from the current optimisation run:\n" + feedback


def build_prompt(record, adapter, feedback=None):
    """
    Builds the (system, prompt) pair process() would send, without
    actually calling the LLM - shared by process() and the CLI's
    `inspect-payload` preview command, so both stay in sync with exactly
    one implementation of "what goes in the prompt".
    """
    code_lines = record.get("code") or []
    prompt_fields = adapter.prompt_fields(code_lines)

    objective = record.get("objective") or "speed"
    prompt_spec = adapter.get_prompts(objective)
    prompt = prompt_spec.template.format(**prompt_fields)

    sections = _build_keep_sections(record)
    feedback_section = _build_feedback_section(feedback)
    if feedback_section:
        sections.append(feedback_section)
    if sections:
        prompt = prompt + "\n\n" + "\n\n".join(sections)

    return prompt_spec.system, prompt


def process(json_path, record, adapter, feedback=None):
    """
    Sends `record` (a tracked function's JSON) to an LLM for refactoring
    suggestions, and writes the result into a separate refactored.json file
    (in a subdirectory named after the same hash as json_path), versioned
    "1", "2", ... and linked back to the original via source_hash/source_json.
    Prompt content (system/template/field values) comes from `adapter`, so
    this module has no language-specific text of its own. `feedback`, if
    given, is plain text assembled by the caller (core.optimise()'s
    iterative mode) - this module doesn't build or interpret it, just
    appends it to the prompt.
    """
    code_lines = record.get("code") or []
    system, prompt = build_prompt(record, adapter, feedback=feedback)

    lines = _active_provider.call(system, prompt)
    lines = _strip_trailing_blank(lines)
    if not lines:
        lines = list(code_lines)

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
        "code": lines,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        # Exactly what was sent to produce this version - already anonymized
        # if privacy_mode was on, real code otherwise. process() itself
        # doesn't know or care which; it just records what it was actually
        # given and sent, so this is a faithful record either way.
        "sent_system": system,
        "sent_prompt": prompt,
        "sent_code": code_lines,
        # The raw LLM response, before any de-anonymization - never itself
        # overwritten (core.submit()'s privacy branch only ever overwrites
        # "code" via storage.overwrite_refactored_version_code), so this
        # stays the permanent record of exactly what came back over the wire.
        "received_code": lines,
    }

    with open(refactored_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

    return lines
