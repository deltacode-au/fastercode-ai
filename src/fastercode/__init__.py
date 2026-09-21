"""fastercode: make your runtime as fast as possible."""

from fastercode.exceptions import FastercodeError
from .core import refactor

# Agent mode is reached through the MCP server (fastercode.mcp_server), not
# by importing a class - which is why its machinery lives in _agent.py rather
# than agent.py. Leaving it importable as `from fastercode import agent`
# would hand back the module (Python resolves a submodule that way even
# without an export here) and fail confusingly at the call. The MCP tool
# descriptions carry the workflow contract - which recording strategy to use,
# and that the user gets asked - and none of that survives a direct import.

__version__ = "0.1.1"
__author__ = "Kyle Winkler"
__email__ = "winkler.kyle@outlook.com"

__all__ = ["FastercodeError", "refactor", "__version__"]
