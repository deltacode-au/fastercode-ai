# Changelog

All notable changes to fastercode are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-08-15

The headline is agent mode: an MCP server that lets a coding agent measure its own rewrites
instead of asserting they are faster.

### Added

- **MCP server** (`fastercode.mcp_server`), exposing five tools:
  - `analyse_function` — which recording strategies apply to a function, best first
  - `optimise_function` — measure candidate rewrites and keep the best correct one
  - `list_runs` / `get_run` — read back the measurements behind a report
  - `swap_version` — write a specific recorded version back into the source file

  Agent mode **calls no LLM and needs no API key**. The agent writes the candidates;
  fastercode only decides whether they are actually better.

- **`fastercode-mcp` console script.** The server also runs as
  `python -m fastercode.mcp_server`.

- **MCP installed standard** — `pip install fastercode-ai` gets you agent mode, no separate
  extra. `fastercode.mcp_server` is still never imported from `__init__.py`, so the
  decorator and `job.yaml` paths pay nothing beyond install size for it.

- **`constraints` on `optimise_function`**, so agent mode can cap the two metrics that are
  not the objective — for example `{"memory": {"max_relative": 1.2}}` alongside
  `objective="speed"`. Each metric takes exactly one of `max` (absolute) or `max_relative`
  (a multiplier on the baseline). Previously constraints were reachable from the Python API
  only, and a constraint passed to the MCP tool was silently ignored.

- **`AGENTS.md`**, the setup and workflow guide for agent mode.

### Fixed

- **Entry-strategy replays could measure the wrong function.** During a replay every
  `@track`-decorated function in the subprocess wrote to a single file that holds one
  function's record, so a script tracking a second function overwrote the record for the
  function being optimised. The harness then compared the target's recorded outputs against
  another function's replayed outputs — usually aborting the run with "did not reproduce all
  N recorded call(s)", but capable of producing a measurement of the wrong function where
  the two outputs happened to be comparable. Only the function being optimised records
  during a replay now; others call through untouched, so nothing leaks into the persistent
  `tracking.json` either. Affected any script that tracked more than one function, including
  the bundled examples.

### Changed

- **Minimum Python is now 3.10** (was 3.9). The 3.9 floor was never real: `adapters/base.py`
  has always used `X | None` annotations in dataclass fields and method signatures, and
  those are evaluated at import time, so the package could not import on 3.9 regardless of
  what the metadata claimed. The floor now states what the code actually requires. The
  `mcp` extra's `python_version >= '3.10'` marker is gone as redundant.

- Output comparison no longer relies on `json.decoder` reusing a single `NaN` object. This
  is internal hardening with **no behavioural change** — NaN-containing outputs already
  compared correctly, via an identity shortcut that happens to hold rather than any
  documented guarantee. Positional NaN is now explicitly treated as reproduced; every other
  comparison remains exactly as strict, including the float equality that makes
  accumulation-order drift a genuine rejection.
