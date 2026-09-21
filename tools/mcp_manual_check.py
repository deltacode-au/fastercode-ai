"""
Manual end-to-end check of the fastercode MCP server, over real stdio.

Spawns `fastercode-mcp` as a subprocess, speaks MCP to it, lists the tools and
calls them against examples/target.py. Use this when the official Inspector
(`npx @modelcontextprotocol/inspector fastercode-mcp`) isn't available - it
exercises the same transport and the same tool schemas, just without a GUI.

    python tools/mcp_manual_check.py            # analyse only (no code changes)
    python tools/mcp_manual_check.py --optimise # also run a real optimise

--optimise REWRITES examples/target.py, so it is opt-in. The file is restored
afterwards; `git diff examples/target.py` should come back empty either way.
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "examples" / "target.py"
FUNCTION = "calculate_average"
ENTRY_POINT = "run_pipeline"

# One good candidate and one deliberately wrong one. The wrong one being
# REJECTED is the point of the exercise, not a failure of the run.
GOOD = "def calculate_average(values):\n    return sum(values) / len(values)"
WRONG = "def calculate_average(values):\n    return 0"


def show(title, payload):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    print(text[:2500] + ("\n... (truncated)" if len(text) > 2500 else ""))


def text_of(result):
    return "".join(c.text for c in result.content if getattr(c, "type", None) == "text")


async def main(do_optimise):
    params = StdioServerParameters(command="fastercode-mcp", args=[], cwd=str(REPO))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()

            info = init.server_info
            print("connected to:", info.name, info.version)
            instructions = getattr(init, "instructions", None)
            print("\n--- server instructions (what the agent is told) ---")
            print(instructions or "<none>")

            tools = (await session.list_tools()).tools
            print("\n--- tools (%d) ---" % len(tools))
            for t in tools:
                # mcp 2.x renamed these to snake_case; fall back for 1.x.
                schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", {})
                required = (schema or {}).get("required", [])
                summary = ((t.description or "").strip().splitlines() or ["<no description>"])[0]
                print("  %-18s required=%s" % (t.name, required))
                print("      %s" % summary)

            show(
                "analyse_function(%s, %s)" % (TARGET.name, FUNCTION),
                text_of(await session.call_tool("analyse_function", {
                    "file": str(TARGET), "function": FUNCTION,
                })),
            )

            if not do_optimise:
                print("\nStopping here. Re-run with --optimise to measure candidates.")
                return

            backup = TARGET.with_suffix(".py.manualcheck")
            shutil.copyfile(TARGET, backup)
            try:
                show(
                    "optimise_function (1 good candidate + 1 deliberately wrong)",
                    text_of(await session.call_tool("optimise_function", {
                        "file": str(TARGET), "function": FUNCTION,
                        "candidates": [GOOD, WRONG],
                        "recording": "entry", "entry_point": ENTRY_POINT,
                        "objective": "speed", "mode": "independent",
                    })),
                )
                runs = text_of(await session.call_tool("list_runs", {
                    "file": str(TARGET), "function": FUNCTION,
                }))
                show("list_runs", runs)

                parsed = json.loads(runs)
                entries = parsed if isinstance(parsed, list) else parsed.get("runs", [])
                if entries:
                    show("get_run (newest)", text_of(await session.call_tool(
                        "get_run", {"run_dir": entries[0]["run_dir"]})))
            finally:
                shutil.copyfile(backup, TARGET)
                backup.unlink()
                print("\nexamples/target.py restored - `git diff examples/target.py` "
                      "should be empty.")


if __name__ == "__main__":
    asyncio.run(main("--optimise" in sys.argv))
