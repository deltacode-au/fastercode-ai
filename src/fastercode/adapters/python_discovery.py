"""
Python's answers to the AgentAdapter questions - the discovery and
test-running half of agent mode.

Sibling to python_privacy.py and used the same way: PythonAdapter delegates
here in thin methods, so everything that knows about Python's AST, its test
conventions and its import machinery lives in one place. Nothing in _agent.py
should need to know any of it.
"""
import ast
import os
import re
import sys

from .. import storage

_TEST_DIR_HINTS = ("test", "tests")


def describe_function(script_path, function):
    # Source lines and parameter names straight from the file. Every failure
    # mode - unreadable, unparseable, no such function - collapses to None,
    # because they all mean the same thing to the caller: nothing to analyse.
    try:
        with open(script_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return None

    node = storage.find_function_node(tree, function)
    if node is None:
        return None

    return (
        source.splitlines()[node.lineno - 1:node.end_lineno],
        [arg.arg for arg in node.args.args],
    )


def find_callers(script_path, function):
    # Top-level functions in the same file that call the target, in source
    # order - these are the entry points an end-to-end replay can use.
    try:
        with open(script_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return []

    target = function.split(".")[-1]
    callers = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == target:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == target:
                callers.append(node.name)
                break
    return callers


def find_tests_referencing(script_path, function, limit=5):
    # Test files near the script that plausibly exercise the function. A bare
    # name match is far too loose - any test that merely mentions the name in a
    # string or a fixture would qualify, and the caller would put a list of
    # junk options to its user. So a file must ALSO reach the script itself:
    # either it imports the module, or it names the script's file.
    #
    # Still a heuristic, and deliberately the conservative kind: a test that
    # doesn't actually call the target records no calls and fails loudly, so
    # the cost of a miss is lower than the cost of a false positive.
    target = function.split(".")[-1]
    module_name = os.path.splitext(os.path.basename(script_path))[0]
    # Guarded so 'target.py' doesn't match inside 'sample_target.py'.
    file_pattern = re.compile(
        r"(?:^|[\\/'\"\s(])" + re.escape(os.path.basename(script_path))
    )
    roots = set()
    directory = os.path.dirname(script_path)
    roots.add(directory)
    parent = os.path.dirname(directory)
    if parent and parent != directory:
        roots.add(parent)
        for hint in _TEST_DIR_HINTS:
            candidate = os.path.join(parent, hint)
            if os.path.isdir(candidate):
                roots.add(candidate)

    found = []
    for root in sorted(roots):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not (name.startswith("test_") or name.endswith("_test.py")):
                continue
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue
            if target not in content:
                continue
            if os.path.abspath(path) == os.path.abspath(script_path):
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            if not (_imports_module(tree, module_name) or file_pattern.search(content)):
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name.startswith("test"):
                    segment = ast.get_source_segment(content, node) or ""
                    if target in segment or file_pattern.search(segment):
                        found.append(f"{path}::{node.name}")
                        if len(found) >= limit:
                            return found
    return found


def _imports_module(tree, module_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == module_name:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == module_name:
                return True
    return False


def run_test(script_path, function, test_id, wrapped):
    # pytest imports the module under test by name, so patching the runpy
    # namespace the caller built is invisible to it. Import the script
    # properly into sys.modules and patch it there instead, so the test's own
    # import sees the tracked wrapper.
    try:
        import pytest
    except ImportError:
        raise ValueError(
            "recording='test' requires pytest to be installed"
        )

    module = import_as_module(script_path)
    target_name = function.split(".")[0]
    original = getattr(module, target_name, None)
    if original is None:
        raise ValueError(f"'{target_name}' not found in the imported {script_path}")

    method_name = None
    if "." in function:
        method_name = function.split(".", 1)[1]
        # Save the class's OWN attribute, not what getattr resolves: for an
        # inherited method the undo is to remove ours again, not to write
        # the parent's copy into the subclass.
        had_own_method = method_name in vars(original)
        previous_method = vars(original).get(method_name)
        setattr(original, method_name, wrapped)
    else:
        setattr(module, target_name, wrapped)

    # pytest imports test modules into sys.modules and reuses them, so in a
    # long-lived process (the MCP server) a second recording would re-run
    # the FIRST version of a test file edited in between - silently
    # measuring a workload that is no longer there. Evict it first.
    drop_cached_module(test_id.split("::")[0])

    try:
        exit_code = pytest.main(["-q", "-p", "no:cacheprovider", test_id])
    finally:
        if method_name is not None:
            if had_own_method:
                setattr(original, method_name, previous_method)
            else:
                delattr(original, method_name)
        else:
            setattr(module, target_name, original)

    if exit_code != 0:
        raise ValueError(
            f"the test '{test_id}' did not pass (pytest exit code {exit_code}) - "
            f"fastercode can only measure against calls that a passing test made"
        )


def import_as_module(script):
    import importlib
    import importlib.util

    script_path = os.path.abspath(script)
    directory = os.path.dirname(script_path)
    module_name = os.path.splitext(os.path.basename(script_path))[0]

    # The test about to run imports this module by name, and may import
    # its siblings, so its directory has to be importable - and has to be
    # FIRST, or a sibling resolves to whichever project was recorded
    # earlier in this process.
    if directory in sys.path:
        sys.path.remove(directory)
    sys.path.insert(0, directory)

    cached = sys.modules.get(module_name)
    cached_file = getattr(cached, "__file__", None) if cached is not None else None
    if cached_file and os.path.abspath(cached_file) == script_path:
        return importlib.reload(cached)

    # Load from the explicit path rather than by name. Module names are
    # basenames, so two projects that both have a target.py collide, and
    # import_module() would hand back whichever directory sits earlier on
    # sys.path instead of the script actually asked for.
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"could not import {script} as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def drop_cached_module(path):
    # Removes any sys.modules entry backed by `path`. Matched on the resolved
    # file rather than the module name because pytest imports a test inside a
    # package under its dotted name, which no basename guess would find.
    resolved = os.path.abspath(path)
    for name, module in list(sys.modules.items()):
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        try:
            same = os.path.abspath(module_file) == resolved
        except (TypeError, ValueError):
            continue
        if same:
            del sys.modules[name]
