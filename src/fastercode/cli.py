import argparse
import datetime
import hashlib
import json
import sys

from . import storage
from .adapters.python_adapter import PythonAdapter


def _cli_swap(file_path, function_name, version, results_dir=None):
    json_path = storage.find_tracking_json(file_path, function_name, json_dir=results_dir)
    if json_path is None:
        print(
            f"No tracked data found for '{function_name}' in '{file_path}' "
            f"(looked under {storage.resolve_data_dir(results_dir)}/*/tracking.json)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        backup_path = storage.apply_swap(json_path, version, PythonAdapter())
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Swapped version {version} into {file_path}::{function_name}")
    print(f"Backup saved to {backup_path}")


def _cli_inspect_payload(file_path, function_name, results_dir=None):
    # Preview/dry-run: always computes a fresh anonymization from current
    # tracking.json state - no privacy_mode history or prior submit() call
    # required, and nothing is sent anywhere.
    json_path = storage.find_tracking_json(file_path, function_name, json_dir=results_dir)
    if json_path is None:
        print(
            f"No tracked data found for '{function_name}' in '{file_path}' "
            f"(looked under {storage.resolve_data_dir(results_dir)}/*/tracking.json)",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as fh:
        record = json.loads(fh.read())

    adapter = PythonAdapter()
    try:
        anon = adapter.anonymize_record(record)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    presented = dict(record)
    presented["code"] = anon.anonymized_code
    presented["keep_variables"] = anon.anonymized_keep_variables
    presented["keep_LOC"] = anon.anonymized_keep_loc

    from . import llm
    system, prompt = llm.build_prompt(presented, adapter)
    payload = system + "\n\n" + prompt
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    print("=== Payload that would be sent ===")
    print(payload)
    print()
    print("=== Identifier mapping (pseudonym -> real name; local only, never sent) ===")
    for pseudonym, real in sorted(anon.mapping.items()):
        print(f"  {pseudonym} -> {real}")
    print()
    print("=== Constraints attached (anonymized form) ===")
    if anon.anonymized_keep_variables:
        print(f"  keep_variables: {anon.anonymized_keep_variables}")
    if anon.anonymized_keep_loc:
        print(f"  keep_LOC: {anon.anonymized_keep_loc}")
    if not anon.anonymized_keep_variables and not anon.anonymized_keep_loc:
        print("  (none)")
    print()
    print(f"Payload hash (sha256): {payload_hash}")
    print(
        "Remote retention: sent to the configured LLM provider for a single "
        "completion request; fastercode does not control that provider's own "
        "data-retention policy."
    )

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record_path = storage.write_privacy_mapping(json_path, f"inspect_{timestamp}", anon.mapping)
    print(f"\nMapping recorded to {record_path}")


def main():
    parser = argparse.ArgumentParser(prog="refactor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    swap_parser = subparsers.add_parser(
        "swap", help="Swap a refactored version into the original source file"
    )
    swap_parser.add_argument("file", help="Path to the tracked source file")
    swap_parser.add_argument("function", help="Function name (or Class.method) to swap")
    swap_parser.add_argument("version", help="Refactored version number to swap in")
    swap_parser.add_argument(
        "--results-dir", default=None,
        help="Directory tracked data lives under (default: FASTERCODE_RESULTS_DIR env var, or .fastercode)",
    )

    inspect_parser = subparsers.add_parser(
        "inspect-payload",
        help="Preview the anonymized payload privacy_mode would send to the LLM",
    )
    inspect_parser.add_argument("file", help="Path to the tracked source file")
    inspect_parser.add_argument("function", help="Function name to inspect")
    inspect_parser.add_argument(
        "--results-dir", default=None,
        help="Directory tracked data lives under (default: FASTERCODE_RESULTS_DIR env var, or .fastercode)",
    )

    args = parser.parse_args()

    if args.command == "swap":
        _cli_swap(args.file, args.function, args.version, results_dir=args.results_dir)
    elif args.command == "inspect-payload":
        _cli_inspect_payload(args.file, args.function, results_dir=args.results_dir)


if __name__ == "__main__":
    main()
