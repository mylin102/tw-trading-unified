# 2026-08-04 Gemini CLI: CI Guard for Streamlit UI f-string Subscript Key Safety
import ast
import glob
import pytest

class UndefinedFStringSubscriptVisitor(ast.NodeVisitor):
    def __init__(self, filename):
        self.filename = filename
        self.issues = []
        self.scope_stack = [set()]

    def visit_FunctionDef(self, node):
        args = {a.arg for a in node.args.args}
        self.scope_stack.append(args.copy())
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Lambda(self, node):
        args = {a.arg for a in node.args.args}
        self.scope_stack.append(args.copy())
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.scope_stack[-1].add(target.id)
        self.generic_visit(node)

    def visit_FormattedValue(self, node):
        for child in ast.walk(node.value):
            if isinstance(child, ast.Subscript):
                if isinstance(child.slice, ast.Name):
                    var_name = child.slice.id
                    # Check if var_name is in any active scope stack
                    defined = any(var_name in scope for scope in self.scope_stack)
                    if not defined:
                        self.issues.append((child.lineno, var_name))
        self.generic_visit(node)


def test_no_undefined_dict_keys_in_ui_fstrings():
    """CI Test: Ensure no undefined variables are used as dict keys inside f-strings in ui/ directory."""
    ui_files = glob.glob("ui/*.py")
    all_issues = []

    for path in ui_files:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)
        visitor = UndefinedFStringSubscriptVisitor(path)
        visitor.visit(tree)
        for line, var in visitor.issues:
            all_issues.append(f"{path}:{line} -> dict[{var}] is undefined in scope!")

    assert not all_issues, f"Found undefined dict key variables in f-strings:\n" + "\n".join(all_issues)
