# Job YAML reference

A job YAML file describes one refactoring job: which script to run, which
functions inside it to track, and what to do with them afterward (submit for
an LLM refactor, swap a version in). Nothing needs to be hand-decorated in
the target script — `run_job()` dynamically applies tracking to the
functions you name.

## Running a job

From a script that only imports `refactor`:

```python
from fastercode import refactor

optimise = refactor()
optimise.run_job("path/to/job.yaml")
```

Or directly from the command line:

```
python -m fastercode.orchestrator path/to/job.yaml
```

Both do the same thing: they spawn one isolated `python` subprocess that
loads the target script, patches the configured functions, runs the entry
point, and performs any configured `post_run` steps.

## Top-level fields

| Field | Required | Description |
|---|---|---|
| `name` | no | A label used only in log/error messages (e.g. `"job 'name': script not found"`). Not used for anything else. |
| `language` | no (default `python`) | Selects which runner executes the job. Only `"python"` exists today — this is a placeholder for other languages later. |
| `api` | no | LLM provider credentials for this job — see below. Applied before anything else runs, regardless of where you might otherwise expect it in the file. |
| `script` | yes | Path to the target script, resolved relative to the current working directory. |
| `entry_point` | yes | Name of the function called *after* every target below has been patched. This is what actually exercises your code. |
| `objective` | no (default `"speed"`) | `"speed"`, `"loc"`, or `"memory"` — passed to this job's own `refactor(objective=...)` instance, and determines which prompt `submit()` sends to the LLM. |
| `constraints` | no (default: none) | Caps on the metrics *other than* `objective` — see below. |
| `privacy_mode` | no (default `true`) | Anonymize identifiers before any code reaches the LLM. Passed straight to this job's own `refactor(privacy_mode=...)` instance. Set to `false` to send real identifiers. |
| `targets` | no (default: none) | List of functions/methods to dynamically track. See below. |
| `post_run` | no (default: none) | List of steps to run after the entry point returns — `submit`/`swap`/`optimise`. See below. |

## `api`

Sets the LLM credentials (and, optionally, model) `submit()`/`optimise()`
will use for this job — equivalent to calling
`llm.set_credentials(provider, key, model=...)` yourself before
`run_job()`. Applied unconditionally as the very first thing `run_job()`
does, **not** as a `post_run` step — a job listing `action: submit` before
some hypothetical "set the key" step would otherwise silently use the
wrong (or no) key.

| Field | Required | Description |
|---|---|---|
| `provider` | yes | Selects a registered `LLMProvider` from `fastercode/providers/`. Registered today: `"openai"` (`gpt-5`), `"anthropic"` (`claude-opus-5`), `"gemini"` (`gemini-3.5-flash`), `"xai"` (`grok-4.5`), `"deepseek"` (`deepseek-v4-pro`), `"mistral"` (`mistral-large-latest`), `"kimi"` (`kimi-k3`), and `"openrouter"` (`openrouter/auto-beta`). All eight work with a stock install — no extra packages. Override any default with `model:` below. Anything else raises a clear error listing what's supported. |
| `key` | yes | Either `${SOME_ENV_VAR}` (resolved from the environment at runtime — **recommended**, since the YAML file never holds the actual secret) or a literal string (supported, but avoid committing a file containing a real key). |
| `model` | no | Which model the provider should use (e.g. `gpt-4o`) — overrides the provider's own default (`"gpt-5"` for `openai`). Not validated against a list — an invalid model surfaces as a real API error when a call is actually made, not a client-side check. |

```yaml
api:
  provider: openai
  key: ${OPENAI_API_KEY}
  model: gpt-4o
```

If `api` is omitted entirely, behavior is unchanged from before this field
existed: `submit()`/`optimise()` fall back to whatever `OPENAI_API_KEY` is
already set in the environment, using the provider's own default model. If
`model` specifically is omitted, only the provider/key are set — the
provider's default model is left as-is.

## `constraints`

"A second component of `objective`": alongside the one primary metric
`objective` minimizes, `constraints` caps the *other two* metrics. A
constraint on the same metric as `objective` itself is rejected at startup
(it's already being minimized) — constraints only apply to the two
non-primary metrics.

```yaml
objective: speed
constraints:
  loc:
    max: 40            # absolute: LOC must stay 
    <= 40
  memory:
    max_relative: 1.2  # relative: peak memory must stay <= 120% of the recorded baseline
```

Each constrained metric (`speed`, `loc`, or `memory`) needs exactly one of:

| Field | Description |
|---|---|
| `max` | An absolute ceiling in the metric's own unit (ms for speed, non-blank lines for loc, KB for memory). |
| `max_relative` | A multiplier over the function's recorded baseline for that metric (e.g. `1.2` = must not exceed 120% of baseline). |

A candidate that's otherwise correct and improves `objective` is still
rejected if it violates a constraint — recorded as
`"condition": "constraint_violated"` in `json/optimise.json` and the PDF
report, distinct from a wrong-output `"rejected"` or a `"timeout"`.

## `targets[]`

Each entry describes one function or method to wrap with tracking:

| Field | Required | Description |
|---|---|---|
| `name` | yes | A plain function name (module-level, e.g. `calculate_average`), or a dotted `Class.method` name (e.g. `Scaler.scale`) for a method on a class defined in the target script. |
| `keep` | no | Same as `optimise.keep(func, variables=..., lines=...)` — see below. |

`keep`, if present:

| Field | Required | Description |
|---|---|---|
| `variables` | no | List of local variable names to capture on every call. |
| `lines` | no | List of `[start, end]` absolute line-number pairs (as they appear in the source file) to capture verbatim. |

```yaml
targets:
  - name: calculate_average
    keep:
      lines: [[11, 13]]
  - name: Scaler.scale        # dotted name = class method
    keep:
      variables: ["scaled"]
```

## `post_run[]`

Each entry is one step, run in order after the entry point returns. `target`
must match a `name` from `targets[]` above (the function has to have been
patched to be usable here).

**`action: submit`** — equivalent to `optimise.submit(func)`:
```yaml
post_run:
  - action: submit
    target: calculate_average
```

**`action: swap`** — equivalent to `optimise.swap_code(func, version)`:
```yaml
post_run:
  - action: swap
    target: calculate_average
    version: 1        # an integer, or the string "latest"
```
`version: "latest"` resolves to the highest existing version number already
present in that function's `refactored.json`.

**`action: optimise`** — equivalent to `optimise.optimise(func, tries=..., mode=...)`:
```yaml
post_run:
  - action: optimise
    target: calculate_average
    tries: 5
    mode: iterative            # or "independent" (default: iterative)
    use_captured_inputs: false # true restores the old single-call replay (default: false)
    timeout: null               # absolute seconds override (default: null = dynamic)
    timeout_margin: 0.2         # only used when timeout is null (default: 0.2)
```
Automatically submits, swaps, and re-runs the target up to `tries` times,
settling on whichever version is both correct, satisfies any `constraints`
(see above), and is best for `objective` (fastest for `speed`, fewest
non-blank lines for `loc`, lowest peak memory for `memory` — measured via
`tracemalloc`, the peak traced allocation during the call).
`mode: iterative` builds each try on the current best and sends the LLM a
growing history in the prompt: the metric to beat, plus one entry per prior
try in that run - its full code, and specifically why it was kept or
rejected (produced incorrect output; violated a named constraint, with the
actual number and cap; correct but not an improvement; or timed out), or how
it became the new best. Every earlier try's entry stays in the prompt for
the rest of the run, so the model sees the complete accumulated history, not
just the previous attempt. `mode: independent` restarts every try from the
untouched original and sends the same prompt every time (true i.i.d.
sampling, no feedback, no compounding).

**Correctness verification** — both modes check **all** of the target's
recorded call history, not just the most recent call; they only differ in
*how* each historical call gets replayed. By default
(`use_captured_inputs: false`), each candidate is checked by re-running the
*real entry script* fresh (so `if __name__ == "__main__":` guards fire, as
they would running it normally) and comparing every call it naturally makes
against history. This requires the entry script to deterministically call
the target the same number of times, in the same order, every run — true
of [`examples/target.py`](../examples/target.py). Set
`use_captured_inputs: true` to instead reconstruct and directly invoke the
target once per historical call, using each call's own stored inputs —
recommended when the target is a plain function/method with no surrounding
class/broader-object state to reconstruct, since it's simpler and faster
than requiring a real, deterministically-replayable entry script; this mode
also requires every recorded call's inputs/outputs to be plain JSON-native
(described below), which the default mode doesn't need.

**Replay timeout** — each replay subprocess gets
`timeout = STARTUP_ALLOWANCE (3s) + max(recorded_total_execution_time × (1 + timeout_margin), 5s floor)`,
computed from the target's own recorded (real-usage) execution times — not
a fixed constant. Set `timeout:` directly to override this calculation
entirely with an absolute number of seconds. A try that exceeds the budget
is recorded with `"condition": "timeout"` in `json/optimise.json`, distinct
from a wrong-output `"rejected"`.

Each run gets its own timestamped folder under
`.fastercode/<hash>/optimise_runs/`, with the tested code/inputs/outputs/
metrics for the original and every try in `json/optimise.json` (`inputs`/
`outputs` are lists — one entry per recorded/matched call), and the
exact prompts/code actually sent to and received from the LLM (regardless
of `privacy_mode`) kept separately in the sibling `json/llm_transcript.json`
— a network/privacy audit concern, distinct from what was tested.

## Full example

See [`examples/job.yaml`](../examples/job.yaml), which targets
[`examples/target.py`](../examples/target.py) and is run by
[`examples/run.py`](../examples/run.py).

## Known limitations

- **Patching only affects the target script's own module namespace.** If
  some other module already imported the function and calls it via its own
  globals, that call site won't see the patch.
- **Nested/local functions** (defined inside another function) aren't
  reachable by name and can't be targeted.
- **Already-decorated target functions** (e.g. `@lru_cache def f(): ...`)
  will have `track()` wrap the decorator's wrapper, not the underlying
  function — target functions meant for dynamic tracking should be
  undecorated in source.
- **`.fastercode/` output is relative to the process's current working
  directory**, not to the job YAML's location or the target script's
  location — run jobs with your working directory set to the repo root.
- **The default entry-script replay assumes a deterministic call pattern**
  — the entry script must call the target the same number of times, in the
  same order, every run, or a candidate gets rejected on a call-count
  mismatch even if its logic is otherwise correct.
- **`optimise()` uses strict equality (`==`)** to compare a candidate's
  output against the recorded baseline. Refactors that restructure
  floating-point accumulation (comprehensions, reordered sums) can
  legitimately flip low-order bits and get rejected as "incorrect" even
  though the algorithm is sound — there's no tolerant/fuzzy comparison
  mode yet.
- **Peak memory (`objective: memory` / a `memory` constraint) is measured
  per call via `tracemalloc`.** A tracked call made *while another tracked
  call is already being measured* (nested tracking) shares the same trace
  rather than getting an isolated reading — the inner call's own peak isn't
  separated from its caller's in that case.
