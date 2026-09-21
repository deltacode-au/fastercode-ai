import argparse
import sys

import yaml

from . import llm, storage
from .adapters import get_adapter
from .core import refactor


def _resolve_latest_version(func):
    return storage.latest_refactored_version(func.refactor_json_path)


def run_worker(config_path):
    # Runs the single job described by config_path in this process - this is
    # what actually executes inside the subprocess spawned by run_job(), and
    # is also directly invokable for testing. Adapter-driven: this function
    # never special-cases any particular language.
    with open(config_path, "r", encoding="utf-8") as fh:
        job = yaml.safe_load(fh)

    api_cfg = job.get("api")
    if api_cfg:
        llm.set_credentials(api_cfg["provider"], api_cfg["key"], model=api_cfg.get("model"))

    job_label = job.get("name", config_path)
    script_path = job["script"]
    entry_point = job["entry_point"]
    adapter = get_adapter(job.get("language", "python"))

    try:
        context = adapter.load_script(script_path, entry_point)
    except FileNotFoundError:
        print(f"[orchestrator] job '{job_label}': script not found: {script_path}", file=sys.stderr)
        sys.exit(2)
    except LookupError:
        print(
            f"[orchestrator] job '{job_label}': entry_point '{entry_point}' not found in {script_path}",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        optimise = refactor(
            objective=job.get("objective", "speed"), adapter=adapter,
            privacy_mode=job.get("privacy_mode", True),
            constraints=job.get("constraints"),
            results_dir=job.get("results_dir"),
        )
    except ValueError as exc:
        print(f"[orchestrator] job '{job_label}': invalid constraints: {exc}", file=sys.stderr)
        sys.exit(2)
    live_globals = context.live_globals

    wrapped_by_target_name = {}
    for target in job.get("targets", []):
        target_name = target["name"]
        original = adapter.resolve_target(live_globals, target_name)
        if original is None:
            print(f"[orchestrator] job '{job_label}': target '{target_name}' not found", file=sys.stderr)
            sys.exit(2)

        wrapped = optimise.track(original)
        adapter.patch_target(live_globals, target_name, wrapped)

        keep_cfg = target.get("keep")
        if keep_cfg:
            optimise.keep(
                wrapped,
                variables=keep_cfg.get("variables"),
                lines=[tuple(pair) for pair in keep_cfg.get("lines", [])],
            )

        wrapped_by_target_name[target_name] = wrapped

    adapter.call_entry_point(live_globals, entry_point)

    for step in job.get("post_run", []):
        target_name = step["target"]
        func = wrapped_by_target_name.get(target_name)
        if func is None:
            print(
                f"[orchestrator] job '{job_label}': post_run target '{target_name}' "
                f"was not patched (add it under targets:)",
                file=sys.stderr,
            )
            sys.exit(2)

        action = step["action"]
        if action == "submit":
            optimise.submit(func)
        elif action == "swap":
            version = step["version"]
            if version == "latest":
                version = _resolve_latest_version(func)
            optimise.swap_code(func, version)
        elif action == "optimise":
            tries = step["tries"]
            mode = step.get("mode", "iterative")
            optimise.optimise(
                func, tries=tries, mode=mode,
                # entry_point tells optimise()'s default replay how to
                # re-exercise this target: it's only ever dynamically
                # patched onto this undecorated script, never @track
                # -decorated in source, so a bare script run wouldn't
                # invoke it at all - see LanguageAdapter.replay_entry.
                entry_point=entry_point,
                use_captured_inputs=step.get("use_captured_inputs", False),
                timeout=step.get("timeout"),
                timeout_margin=step.get("timeout_margin", 0.2),
            )
        else:
            print(f"[orchestrator] job '{job_label}': unknown post_run action '{action}'", file=sys.stderr)
            sys.exit(2)


def run_job(config_path):
    # Very simple v1: runs exactly the one job described by config_path,
    # dispatching to whichever adapter's run_isolated() the YAML's
    # `language:` field selects (today, only "python" is registered).
    with open(config_path, "r", encoding="utf-8") as fh:
        job = yaml.safe_load(fh)

    try:
        adapter = get_adapter(job.get("language", "python"))
    except ValueError as exc:
        print(f"[orchestrator] {exc}", file=sys.stderr)
        return 1

    return adapter.run_isolated(config_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m fastercode.orchestrator")
    parser.add_argument("config", help="Path to the job YAML config file")
    parser.add_argument(
        "--worker", action="store_true",
        help=argparse.SUPPRESS,  # internal: worker-mode marker
    )

    args = parser.parse_args()

    if args.worker:
        run_worker(args.config)
        sys.exit(0)
    else:
        sys.exit(run_job(args.config))
