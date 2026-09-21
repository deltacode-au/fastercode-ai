import ast
import re
import secrets

from .base import PrivacyError

# --- binding collection -----------------------------------------------------


class _BindingCollector(ast.NodeVisitor):
    # Walks a (single, non-nested) function's body to find every name that's
    # a genuine local binding - the only things safe to rename. Built-ins
    # need no exclusion list: a name only ever Load'd is never added here,
    # which also correctly treats a builtin shadowed by assignment (e.g.
    # `len = 5`) as a real local that should be renamed.
    def __init__(self):
        self.local_names = set()
        self.except_names = set()
        self.global_nonlocal_names = set()

    def visit_FunctionDef(self, node):
        raise PrivacyError(
            "nested function definitions are not supported under privacy_mode"
        )

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        raise PrivacyError("lambda expressions are not supported under privacy_mode")

    def visit_Import(self, node):
        raise PrivacyError("in-function imports are not supported under privacy_mode")

    visit_ImportFrom = visit_Import

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.local_names.add(node.id)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.global_nonlocal_names.update(node.names)

    def visit_Nonlocal(self, node):
        self.global_nonlocal_names.update(node.names)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.except_names.add(node.name)
        self.generic_visit(node)


def _collect_param_names(args):
    names = [a.arg for a in args.posonlyargs] + [a.arg for a in args.args]
    if args.vararg:
        names.append(args.vararg.arg)
    names.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return names


# --- renaming ----------------------------------------------------------------


def _generate_pseudonym(prefix, used):
    while True:
        token = f"{prefix}_{secrets.token_hex(2)}"
        if token not in used:
            used.add(token)
            return token


class _Renamer(ast.NodeTransformer):
    def __init__(self, real_to_pseudo):
        self.real_to_pseudo = real_to_pseudo

    def visit_FunctionDef(self, node):
        node.name = self.real_to_pseudo.get(node.name, node.name)
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arg(self, node):
        node.arg = self.real_to_pseudo.get(node.arg, node.arg)
        return node

    def visit_Name(self, node):
        node.id = self.real_to_pseudo.get(node.id, node.id)
        return node

    def visit_ExceptHandler(self, node):
        if node.name:
            node.name = self.real_to_pseudo.get(node.name, node.name)
        self.generic_visit(node)
        return node


def _strip_docstring(func_node):
    body = func_node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    if not body:
        body.append(ast.Pass())


# --- keep(lines=...) translation ---------------------------------------------


def _sub_bodies(stmt):
    if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        bodies = [stmt.body]
        if stmt.orelse:
            bodies.append(stmt.orelse)
        return bodies
    if isinstance(stmt, ast.Try):
        bodies = [stmt.body]
        bodies.extend(handler.body for handler in stmt.handlers)
        if stmt.orelse:
            bodies.append(stmt.orelse)
        if stmt.finalbody:
            bodies.append(stmt.finalbody)
        return bodies
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [stmt.body]
    return []


def _find_statement_run(body, rel_start, rel_end):
    # Finds a contiguous run of sibling statements at one nesting level
    # whose combined span exactly equals [rel_start, rel_end], searching
    # recursively into if/for/while/try/with sub-blocks. Returns the list
    # of matching ast.stmt nodes, or None.
    n = len(body)
    for i in range(n):
        if body[i].lineno != rel_start:
            continue
        for j in range(i, n):
            if body[j].end_lineno == rel_end:
                return body[i:j + 1]
            if body[j].end_lineno > rel_end:
                break
    for stmt in body:
        for sub_body in _sub_bodies(stmt):
            found = _find_statement_run(sub_body, rel_start, rel_end)
            if found is not None:
                return found
    return None


def _find_sublist_by_stripped_content(haystack, needle):
    # Like storage.find_sublist, but compares each line's stripped content -
    # a statement run's own unparse is only correct in isolation up to its
    # absolute indentation (ast.unparse has no notion of "this block is
    # nested N levels deep in some other function"), so an indentation-
    # insensitive comparison is what actually lets us relocate it inside
    # the whole function's own unparse.
    needle_stripped = [line.strip() for line in needle]
    n = len(needle_stripped)
    if n == 0 or n > len(haystack):
        return None
    for i in range(len(haystack) - n + 1):
        if [line.strip() for line in haystack[i:i + n]] == needle_stripped:
            return i
    return None


# --- public API ----------------------------------------------------------


def anonymize_source(code_lines, keep_loc_ranges, function_start_line):
    """
    Anonymizes a single function's source. Returns
    {"anonymized_code": [...], "anonymized_keep_loc": {...}, "mapping": {...}}
    (mapping is pseudonym -> real name). Raises PrivacyError for v1
    out-of-scope constructs (nested def/lambda/in-function import) or a
    keep_loc range that doesn't bracket a whole statement / contiguous run.
    """
    tree = ast.parse("\n".join(code_lines))
    func_node = tree.body[0]
    if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise PrivacyError("expected a single function definition")

    collector = _BindingCollector()
    for stmt in func_node.body:
        collector.visit(stmt)

    renamable = set(_collect_param_names(func_node.args))
    renamable |= collector.local_names
    renamable |= collector.except_names
    renamable -= collector.global_nonlocal_names

    used_tokens = set()
    real_to_pseudo = {func_node.name: _generate_pseudonym("fn", used_tokens)}
    for name in sorted(renamable):
        real_to_pseudo[name] = _generate_pseudonym("var", used_tokens)

    func_node = _Renamer(real_to_pseudo).visit(func_node)
    ast.fix_missing_locations(func_node)

    # keep_loc block-finding uses the renamed tree's line numbers, which
    # renaming never changes (it only mutates .id/.arg/.name string fields).
    found_blocks = []
    for start, end in (keep_loc_ranges or []):
        rel_start = start - function_start_line + 1
        rel_end = end - function_start_line + 1
        stmts = _find_statement_run(func_node.body, rel_start, rel_end)
        if stmts is None:
            raise PrivacyError(
                f"keep(lines=[{start}, {end}]) does not bracket a whole "
                f"statement (or contiguous run of statements) - privacy_mode "
                f"requires keep_loc ranges to align to complete statements"
            )
        block_lines = []
        for stmt in stmts:
            block_lines.extend(ast.unparse(stmt).splitlines())
        found_blocks.append((start, end, block_lines))

    _strip_docstring(func_node)
    anonymized_code = ast.unparse(func_node).splitlines()

    anonymized_keep_loc = {}
    for start, end, block_lines in found_blocks:
        match_index = _find_sublist_by_stripped_content(anonymized_code, block_lines)
        if match_index is None:
            raise PrivacyError(
                f"could not relocate keep(lines=[{start}, {end}]) in the "
                f"anonymized code"
            )
        new_start = match_index + 1
        new_end = new_start + len(block_lines) - 1
        anonymized_keep_loc[f"{new_start}-{new_end}"] = block_lines

    mapping = {pseudo: real for real, pseudo in real_to_pseudo.items()}
    return {
        "anonymized_code": anonymized_code,
        "anonymized_keep_loc": anonymized_keep_loc,
        "mapping": mapping,
    }


class _Reverser(ast.NodeTransformer):
    def __init__(self, mapping):
        self.mapping = mapping  # pseudonym -> real name

    def visit_FunctionDef(self, node):
        node.name = self.mapping.get(node.name, node.name)
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arg(self, node):
        node.arg = self.mapping.get(node.arg, node.arg)
        return node

    def visit_Name(self, node):
        node.id = self.mapping.get(node.id, node.id)
        return node

    def visit_ExceptHandler(self, node):
        if node.name:
            node.name = self.mapping.get(node.name, node.name)
        self.generic_visit(node)
        return node


def _deanonymize_by_regex(lines, mapping):
    text = "\n".join(lines)
    for pseudonym, real in mapping.items():
        text = re.sub(r"\b" + re.escape(pseudonym) + r"\b", real, text)
    return text.splitlines()


def deanonymize_source(lines, mapping):
    # Deliberately not scope-restricted like anonymize_source - the LLM's
    # response may restructure code in ways the input never did, and
    # reversing known pseudonym tokens doesn't require proving anything
    # about scope. Falls back to word-boundary regex substitution only if
    # the response doesn't parse - safe specifically because these are
    # freshly random tokens that can't coincidentally collide with real code.
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return _deanonymize_by_regex(lines, mapping)

    tree = _Reverser(mapping).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree).splitlines()
