import os

from .base import AgentAdapter, LanguageAdapter
from .python_adapter import PythonAdapter

ADAPTERS = {
    "python": PythonAdapter(),
}

# Only used to guess when a caller didn't say. job.yaml names its language
# outright, and an MCP caller can too - this is the convenience for the common
# case where the path already settles it.
EXTENSIONS = {
    ".py": "python",
}


def get_adapter(name):
    adapter = ADAPTERS.get(name)
    if adapter is None:
        raise ValueError(f"unknown language '{name}'")
    return adapter


def adapter_for_file(path, language=None):
    # An explicit language always wins: an extension is a guess, and some
    # toolchains aren't determined by one.
    if language:
        return get_adapter(language)

    extension = os.path.splitext(path or "")[1].lower()
    name = EXTENSIONS.get(extension)
    if name is None:
        raise ValueError(
            f"could not tell which language '{path}' is from its extension "
            f"(known: {', '.join(sorted(EXTENSIONS))}) - pass language= "
            f"explicitly, one of: {', '.join(sorted(ADAPTERS))}"
        )
    return get_adapter(name)
