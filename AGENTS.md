# Using fastercode as an agent

You write the code. fastercode decides whether it is actually better.

When a user says *"make this function faster"*, the useful output is not the
rewrite — you can produce that on your own. It is the **evidence**: the function's
real calls replayed against your candidate, its outputs checked against the
original's, its cost measured, and a report the user can read afterwards. That is
what this package is for. No LLM is called at any point.

## Setup

Agent mode is reached through the MCP server — there is no Python class to import.

```
pip install fastercode-ai
```

Register it with your harness:

```bash
claude mcp add fastercode -- python -m fastercode.mcp_server
```

or, in an MCP config file:

```json
{
  "mcpServers": {
    "fastercode": {
      "command": "python",
      "args": ["-m", "fastercode.mcp_server"]
    }
  }
}
```

Paths are resolved against the server's working directory, which is whatever your
harness launched it in. **Pass absolute paths** unless you know the server was
started in the project root.

## The workflow

### 1. `analyse_function(file, function)` — find out what is possible

Returns the function's source and parameters, plus the recording strategies that
apply to it, best first, and any previous runs whose report you could read instead
of re-running.

### 2. Ask the user which strategy to use

**Do not choose silently.** The strategy determines what the report is evidence
*of*, and only the user knows which one reflects how their code is really used.

| strategy | what it measures against | when |
|---|---|---|
| `entry` | their script, replayed end to end | **preferred** — `analyse_function` found a function that calls the target |
| `test` | the calls their own test makes | a test exercises the target |
| `inputs` | calls you invent | nothing else is available; weakest evidence, say so |

### 3. `optimise_function(...)` — measure your candidates

```json
{
  "file": "/abs/path/app/stats.py",
  "function": "calculate_average",
  "candidates": ["def calculate_average(values):\n    return sum(values) / len(values)"],
  "recording": "entry",
  "entry_point": "run_pipeline"
}
```

Each candidate must be the **complete** function source, including the `def` line —
every one is swapped into the file and executed on its own.

`mode` defaults to `independent`, which judges every candidate against the
original. `iterative` builds each on the previous best.

### 4. Report back honestly

The returned summary carries `baseline`, `winner_version`, `improvement_pct`, every
attempt's verdict, and `report_path`. Give the user that path.

Report the **rejections too**. A candidate marked `correct: false` produced
different output — that is the tool doing its job, and hiding it defeats the
purpose. If nothing won, say nothing won; the original is left untouched.

Use `get_run(run_dir)` to quote specific measurements back to the user, and
`list_runs()` to point them at earlier evidence.

## What gets written

`.fastercode/<hash>/optimise_runs/<timestamp>/` — `report.pdf`, the full
measurements in `json/optimise.json`, and `backups/` holding the exact code that
was executed for each try. Every attempt records `code_sha256`, the hash of the
bytes that produced its numbers, so a reviewer can check a claim against what
really ran.

## Measurement caveat — read this before quoting a number

Each candidate is measured by **one** replay. On a fast function the run-to-run
variance can exceed the difference between candidates, so a small "win" or "loss"
may be noise. Prefer recording strategies whose calls do enough work to measure,
and do not present a sub-microsecond difference to the user as a speedup.
