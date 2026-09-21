# Agent mode — homepage copy

Source material for the fastercode.ai homepage. Each section stands alone and can be moved
onto a page more or less as written.

**Describes fastercode 0.2.1.** Not yet published — don't put install instructions live
until the upload happens.

---

## You write the code. fastercode decides whether it's actually better.

---

## The half that's missing

Your coding agent will happily tell you its rewrite is faster. Nothing checks.

fastercode is the other half. It takes the candidate your agent wrote, swaps it into your
file, replays the function's **real recorded calls**, checks the outputs still match the
original's, measures what it cost, and keeps it only if it is genuinely both correct and
better.

It is not a code generator. It is the thing that tells your code generator it was wrong.

---

## No LLM. No API key. Nothing leaves your machine.

You already have a model — Claude Code, Cursor, Copilot. You don't need fastercode to call a
second one.

Agent mode makes **zero model calls**. Your agent writes the candidates; fastercode only
judges them. There's no key to configure, no vendor to trust with your source, and no
per-token cost to think about.

Every run records its provider as `agent`, so a report can never name a model that was
never asked.

---

## How it works

1. **`analyse_function`** — fastercode reports which recording strategies are actually
   available for your function.
2. **Your agent asks you which one to use.** It is instructed not to choose silently,
   because that choice determines what the report is evidence *of*.
3. **`optimise_function`** — every candidate is swapped in, executed, and measured against
   the recorded calls.
4. **You get a report** — and a straight answer about which candidates failed.

---

## Evidence, not opinion

Every run writes a folder you can audit afterwards:

| What | Where |
|---|---|
| The report | `report.html` |
| Full measurements — every attempt, verdict and output | `json/optimise.json` |
| The exact code executed for each try | `backups/` |
| A `code_sha256` per attempt | in the measurements |

That last one matters: every number is tied to a hash of the bytes that produced it. If a
result looks surprising, you can check the claim against what actually ran.

---

## Three kinds of evidence — and it tells you which you're getting

| Strategy | Measured against | Strength |
|---|---|---|
| `entry` | your own script, replayed end to end | **Strongest** — real usage |
| `test` | the calls your own test makes | Strong — your test, your workload |
| `inputs` | calls the agent invents | Weakest, and it will say so |

A benchmark is only worth what its inputs are worth. fastercode won't quietly pick the
convenient one and present the result as though it came from your real workload.

---

## Ask for what you actually want

**Objective** — optimise for `speed`, `loc` (lines of code), or `memory` (peak usage).

**Constraints** — cap the two metrics you *didn't* pick. `{"memory": {"max_relative": 1.2}}`
means a faster candidate that uses more than 1.2× the baseline's peak memory doesn't count
as a win. Each metric takes either `max` (an absolute ceiling) or `max_relative` (a
multiplier on your baseline).

**Mode** — `independent` judges every candidate against the original; `iterative` builds
each one on the best so far.

So *"make it faster, but don't use more than 1.2× the memory"* is a thing you can simply
say.

---

## Rejection is the product

A candidate whose outputs differ from the original is **rejected**, and reported as
rejected. One that meets the objective but breaks a constraint is recorded as
`constraint_violated` — not quietly counted as a win.

If nothing wins, your original code is left exactly as it was.

Your agent is told to report the rejections alongside the wins. A tool that only ever
confirms what the agent already believed would be worth nothing.

---

## Five tools

| Tool | What it does | Touches your code |
|---|---|---|
| `analyse_function` | Which strategies apply, the function's source, earlier runs worth reading | No |
| `optimise_function` | Measures the candidates, applies the best correct improvement | **Yes** |
| `list_runs` | Every run on disk, newest first | No |
| `get_run` | Full measurements behind any report | No |
| `swap_version` | Restores a specific measured version | **Yes** |

Only two write anything. The rest just read back evidence.

---

## Setup

```
pip install fastercode-ai
claude mcp add fastercode -- python -m fastercode.mcp_server
```

Or in an MCP config file:

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

An installed `fastercode-mcp` console script does the same thing as `python -m
fastercode.mcp_server` — use whichever your harness resolves more reliably.

Then just ask: *"use fastercode to make `calculate_average` faster."*

---

## Requirements, and one honest caveat

Python 3.10 or newer. Python is the supported language.

**On precision:** each candidate is measured by one replay. On a fast function the
run-to-run variance can exceed the difference between candidates, so a small win or loss may
be noise. Prefer recording strategies whose calls do enough work to measure, and treat a
sub-microsecond difference as no difference.

A measurement tool that overstated its own precision would be undercutting the only thing
it's for.
