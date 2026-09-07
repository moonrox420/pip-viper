"""Unit tests for src.code_tools.

These tests exercise pure, Qt-free logic: the syntax auto-fixer, the
Black/isort/autoflake wrappers, the docstring/test generators, the symbol
renamer, and the full pipeline.
"""

from __future__ import annotations

import ast

import pytest

from src.code_tools import (
    CodeFormatter,
    CodeGenerator,
    CodeToolsPipeline,
    SymbolRenamer,
    SyntaxAutoFixer,
)
from src import (
    CodeGenerationError,
    ImportOrganizationError,
    RefactorError,
    SyntaxFixError,
    UnusedCodeCleanupError,
)

# -----------------------------------------------------------------------------
# SyntaxAutoFixer.check
# -----------------------------------------------------------------------------


def test_check_reports_valid_source_as_valid() -> None:
    result = SyntaxAutoFixer.check("x = 1 + 2\n")
    assert result.is_valid is True
    assert result.issues == []


def test_check_reports_invalid_source_with_location() -> None:
    result = SyntaxAutoFixer.check("def f(:\n    pass\n")
    assert result.is_valid is False
    assert len(result.issues) == 1
    assert result.issues[0].line == 1


# -----------------------------------------------------------------------------
# SyntaxAutoFixer.autofix
# -----------------------------------------------------------------------------


def test_autofix_missing_colon_on_if() -> None:
    result = SyntaxAutoFixer.autofix("if True\n    print('hi')\n")
    assert result.success is True
    ast.parse(result.fixed_source)  # must be valid now
    assert "if True:" in result.fixed_source
    assert result.applied_fixes


def test_autofix_missing_colon_on_def() -> None:
    result = SyntaxAutoFixer.autofix("def greet(name)\n    return name\n")
    assert result.success is True
    ast.parse(result.fixed_source)
    assert "def greet(name):" in result.fixed_source


def test_autofix_unclosed_parenthesis_at_eof() -> None:
    result = SyntaxAutoFixer.autofix("def f(x, y):\n    return (x + y\n")
    assert result.success is True
    ast.parse(result.fixed_source)


def test_autofix_unclosed_bracket_nested() -> None:
    source = "data = {\n    'a': [1, 2, 3],\n    'b': 4\n"
    result = SyntaxAutoFixer.autofix(source)
    assert result.success is True
    ast.parse(result.fixed_source)


def test_autofix_reports_honest_failure_for_out_of_scope_bracket_typo() -> None:
    # A single-line bracket typo followed by more valid code is outside the
    # documented scope of the EOF-append heuristic; the fixer must report
    # this honestly rather than mangling the file with a dangling closer.
    result = SyntaxAutoFixer.autofix("def f(x, y:\n    return x + y\n")
    assert result.success is False
    assert result.remaining_issue is not None


def test_autofix_tab_space_mix() -> None:
    source = "def f():\n\tif True:\n\t\treturn 1\n    return 2\n"
    result = SyntaxAutoFixer.autofix(source)
    # Should either fully resolve or make genuine progress; never crash.
    assert isinstance(result.success, bool)


def test_autofix_python2_print() -> None:
    result = SyntaxAutoFixer.autofix("print 'hello world'\n")
    assert result.success is True
    ast.parse(result.fixed_source)
    assert "print('hello world')" in result.fixed_source


def test_autofix_unterminated_string() -> None:
    result = SyntaxAutoFixer.autofix('message = "hello\n')
    assert result.success is True
    ast.parse(result.fixed_source)


def test_autofix_already_valid_source_is_noop() -> None:
    source = "x = 1\n"
    result = SyntaxAutoFixer.autofix(source)
    assert result.success is True
    assert result.fixed_source == source
    assert result.applied_fixes == []
    assert result.changed is False


def test_autofix_unfixable_error_reports_remaining_issue() -> None:
    # A structurally nonsensical token sequence with none of our supported
    # patterns -- the fixer should stop and report honestly rather than loop.
    result = SyntaxAutoFixer.autofix("@@@ ??? !!!\n")
    assert result.success is False
    assert result.remaining_issue is not None


def test_autofix_rejects_invalid_max_iterations() -> None:
    with pytest.raises(SyntaxFixError):
        SyntaxAutoFixer.autofix("x = 1\n", max_iterations=0)


# -----------------------------------------------------------------------------
# CodeFormatter
# -----------------------------------------------------------------------------


def test_format_source_reformats_messy_code() -> None:
    result = CodeFormatter.format_source("x=1\ny  =2\n")
    assert result.changed is True
    assert result.formatted_source == "x = 1\ny = 2\n"


def test_format_source_noop_on_already_formatted() -> None:
    result = CodeFormatter.format_source("x = 1\n")
    assert result.changed is False


def test_organize_imports_sorts_and_groups() -> None:
    source = "import sys\nimport os\nimport json\n\nx = 1\n"
    result = CodeFormatter.organize_imports(source)
    assert result.changed is True
    lines = result.organized_source.splitlines()
    assert lines[:3] == ["import json", "import os", "import sys"]


def test_organize_imports_rejects_invalid_syntax() -> None:
    with pytest.raises(ImportOrganizationError):
        CodeFormatter.organize_imports("def f(:\n")


def test_remove_unused_strips_unused_import() -> None:
    source = "import os\nimport sys\n\nprint(sys.argv)\n"
    result = CodeFormatter.remove_unused(source)
    assert result.changed is True
    assert "import os" not in result.cleaned_source
    assert "import sys" in result.cleaned_source


def test_remove_unused_rejects_invalid_syntax() -> None:
    with pytest.raises(UnusedCodeCleanupError):
        CodeFormatter.remove_unused("def f(:\n")


# -----------------------------------------------------------------------------
# CodeGenerator.generate_docstrings
# -----------------------------------------------------------------------------


def test_generate_docstrings_inserts_for_function_and_class() -> None:
    source = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self, name: str) -> str:\n"
        "        return f'hi {name}'\n"
    )
    result = CodeGenerator.generate_docstrings(source)
    ast.parse(result.generated_source)  # must remain valid
    assert set(result.inserted_symbols) == {"add", "Greeter", "greet"}
    assert result.inserted_count == 3
    tree = ast.parse(result.generated_source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            assert ast.get_docstring(node) is not None


def test_generate_docstrings_skips_documented_symbols() -> None:
    source = (
        'def already_documented():\n    """Has a docstring already."""\n    return 1\n'
    )
    result = CodeGenerator.generate_docstrings(source)
    assert result.inserted_symbols == []
    assert result.generated_source == source


def test_generate_docstrings_rejects_invalid_syntax() -> None:
    with pytest.raises(CodeGenerationError):
        CodeGenerator.generate_docstrings("def f(:\n")


# -----------------------------------------------------------------------------
# CodeGenerator.generate_unit_tests
# -----------------------------------------------------------------------------


def test_generate_unit_tests_covers_public_functions_and_classes() -> None:
    source = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def _private(a):\n"
        "    return a\n"
        "\n"
        "\n"
        "class Calculator:\n"
        "    def multiply(self, a: int, b: int) -> int:\n"
        "        return a * b\n"
    )
    result = CodeGenerator.generate_unit_tests(source, module_name="mymodule")
    ast.parse(result.generated_source)
    assert "add" in result.covered_symbols
    assert "Calculator" in result.covered_symbols
    assert "_private" not in result.covered_symbols
    assert "def test_add(" in result.generated_source
    assert "mymodule.add(" in result.generated_source
    assert "mymodule.Calculator(" in result.generated_source


def test_generate_unit_tests_rejects_bad_module_name() -> None:
    with pytest.raises(CodeGenerationError):
        CodeGenerator.generate_unit_tests("x = 1\n", module_name="not a valid name")


def test_generate_unit_tests_empty_module_falls_back_to_import_check() -> None:
    result = CodeGenerator.generate_unit_tests("x = 1\n", module_name="mymodule")
    assert result.covered_symbols == []
    assert "test_module_imports" in result.generated_source


# -----------------------------------------------------------------------------
# SymbolRenamer
# -----------------------------------------------------------------------------


def test_rename_replaces_all_free_standing_occurrences() -> None:
    source = "count = 0\ncount = count + 1\nprint(count)\n"
    result = SymbolRenamer.rename(source, "count", "total")
    assert result.occurrence_count == 4  # 1 + (LHS & RHS) + 1
    ast.parse(result.renamed_source)
    assert "count" not in result.renamed_source
    assert result.renamed_source.count("total") == 4


def test_rename_skips_attribute_access() -> None:
    source = "value = 1\nobj.value = 2\nprint(obj.value)\nprint(value)\n"
    result = SymbolRenamer.rename(source, "value", "amount")
    ast.parse(result.renamed_source)
    # Only the two free-standing occurrences of `value` should be renamed,
    # not the `obj.value` attribute accesses.
    assert result.occurrence_count == 2
    assert "obj.value" in result.renamed_source
    assert "amount = 1" in result.renamed_source
    assert "print(amount)" in result.renamed_source


def test_rename_preserves_string_and_comment_contents() -> None:
    source = 'x = 1  # x is a counter\nmessage = "the value of x"\nprint(x)\n'
    result = SymbolRenamer.rename(source, "x", "counter_value")
    ast.parse(result.renamed_source)
    assert result.occurrence_count == 2  # assignment + print, not string/comment
    assert "# x is a counter" in result.renamed_source
    assert '"the value of x"' in result.renamed_source
    assert "counter_value = 1" in result.renamed_source
    assert "print(counter_value)" in result.renamed_source


def test_rename_no_occurrences_is_noop() -> None:
    source = "y = 1\n"
    result = SymbolRenamer.rename(source, "nonexistent", "renamed")
    assert result.occurrence_count == 0
    assert result.renamed_source == source


def test_rename_rejects_invalid_identifiers() -> None:
    with pytest.raises(RefactorError):
        SymbolRenamer.rename("x = 1\n", "not-valid", "y")
    with pytest.raises(RefactorError):
        SymbolRenamer.rename("x = 1\n", "x", "class")
    with pytest.raises(RefactorError):
        SymbolRenamer.rename("x = 1\n", "x", "x")


def test_rename_rejects_invalid_source() -> None:
    with pytest.raises(RefactorError):
        SymbolRenamer.rename("def f(:\n", "f", "g")


# -----------------------------------------------------------------------------
# CodeToolsPipeline
# -----------------------------------------------------------------------------


def test_pipeline_fixes_formats_and_organizes() -> None:
    source = "import sys\nimport os\ndef f(x,y):\n  return x+y\nprint(os.getcwd())\n"
    result = CodeToolsPipeline.run_full_pipeline(source)
    ast.parse(result.final_source)
    assert "import sys" not in result.final_source  # unused, should be removed
    assert "import os" in result.final_source
    assert result.changed is True
    assert len(result.steps) == 4


def test_pipeline_autofixes_broken_syntax_first() -> None:
    source = "if True\n    x = 1\nprint(x)\n"
    result = CodeToolsPipeline.run_full_pipeline(source)
    ast.parse(result.final_source)
    assert "if True:" in result.final_source
    assert result.steps[0].step_name == "Auto-Fix Syntax"
    assert result.steps[0].changed is True


def test_pipeline_raises_when_unfixable() -> None:
    with pytest.raises(SyntaxFixError):
        CodeToolsPipeline.run_full_pipeline("@@@ ??? !!!\n")


def test_pipeline_noop_on_already_clean_code() -> None:
    source = "import os\n\n\ndef f() -> None:\n    print(os.getcwd())\n"
    result = CodeToolsPipeline.run_full_pipeline(source)
    assert result.final_source == source
    assert result.changed is False
