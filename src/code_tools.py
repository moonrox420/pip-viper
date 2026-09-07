"""Python auto syntax fixer, formatter, refactorer, and code generator.

This module is the backend for PipViper's "Code Tools" panel, which replaces
the previous debugpy-based debugger with a fully local, deterministic suite
of source-transformation utilities.
"""

from __future__ import annotations

import ast
import enum
import io
import keyword
import logging
import re
import tokenize
from typing import Callable

import autoflake
import black
import isort
from pydantic import BaseModel, ConfigDict, Field

from . import (
    CodeGenerationError,
    FormattingError,
    ImportOrganizationError,
    RefactorError,
    SyntaxFixError,
    UnusedCodeCleanupError,
)

_LOGGER: logging.Logger = logging.getLogger("src.code_tools")

__all__ = [
    "FixSeverity",
    "CodeIssue",
    "SyntaxCheckResult",
    "SyntaxFixResult",
    "FormatResult",
    "ImportOrganizeResult",
    "UnusedCleanupResult",
    "DocstringGenerationResult",
    "TestGenerationResult",
    "RenameResult",
    "PipelineStepResult",
    "PipelineResult",
    "SyntaxAutoFixer",
    "CodeFormatter",
    "CodeGenerator",
    "SymbolRenamer",
    "CodeToolsPipeline",
]

DEFAULT_LINE_LENGTH: int = 100
_MAX_AUTOFIX_ITERATIONS: int = 25


# -----------------------------------------------------------------------------
# Result & Payload Models
# -----------------------------------------------------------------------------


class FixSeverity(str, enum.Enum):
    """Severity classification for a code-tools diagnostic."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class CodeIssue(BaseModel):
    """A single diagnostic surfaced by the syntax checker or auto-fixer."""

    model_config = ConfigDict(frozen=True)

    line: int = Field(ge=1)
    column: int = Field(ge=0)
    message: str
    severity: FixSeverity


class SyntaxCheckResult(BaseModel):
    """Outcome of a read-only syntax validation pass."""

    model_config = ConfigDict(frozen=True)

    is_valid: bool
    issues: list[CodeIssue] = Field(default_factory=list)


class SyntaxFixResult(BaseModel):
    """Outcome of an iterative auto-fix pass over broken source code."""

    model_config = ConfigDict(frozen=True)

    success: bool
    original_source: str
    fixed_source: str
    applied_fixes: list[str] = Field(default_factory=list)
    remaining_issue: CodeIssue | None = None

    @property
    def changed(self) -> bool:
        """Return True if the fixer modified the source at all."""
        return self.fixed_source != self.original_source


class FormatResult(BaseModel):
    """Outcome of a Black formatting pass."""

    model_config = ConfigDict(frozen=True)

    original_source: str
    formatted_source: str
    changed: bool


class ImportOrganizeResult(BaseModel):
    """Outcome of an isort import-organization pass."""

    model_config = ConfigDict(frozen=True)

    original_source: str
    organized_source: str
    changed: bool


class UnusedCleanupResult(BaseModel):
    """Outcome of an autoflake unused-import/variable cleanup pass."""

    model_config = ConfigDict(frozen=True)

    original_source: str
    cleaned_source: str
    changed: bool


class DocstringGenerationResult(BaseModel):
    """Outcome of generating missing docstrings for functions/classes."""

    model_config = ConfigDict(frozen=True)

    original_source: str
    generated_source: str
    inserted_symbols: list[str] = Field(default_factory=list)

    @property
    def inserted_count(self) -> int:
        """Return the number of docstrings that were inserted."""
        return len(self.inserted_symbols)


class TestGenerationResult(BaseModel):
    """Outcome of generating a pytest unit test skeleton for a module."""

    model_config = ConfigDict(frozen=True)

    generated_source: str
    covered_symbols: list[str] = Field(default_factory=list)


class RenameResult(BaseModel):
    """Outcome of a whole-file lexical symbol rename."""

    model_config = ConfigDict(frozen=True)

    original_source: str
    renamed_source: str
    occurrence_count: int


class PipelineStepResult(BaseModel):
    """A single step's contribution to a full code-tools pipeline run."""

    model_config = ConfigDict(frozen=True)

    step_name: str
    changed: bool
    detail: str


class PipelineResult(BaseModel):
    """Aggregate outcome of running the full code-tools pipeline."""

    model_config = ConfigDict(frozen=True)

    original_source: str
    final_source: str
    steps: list[PipelineStepResult] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Return True if the pipeline modified the source at all."""
        return self.final_source != self.original_source


# -----------------------------------------------------------------------------
# Shared Helpers
# -----------------------------------------------------------------------------


def _syntax_error_to_issue(error: SyntaxError) -> CodeIssue:
    """Convert a raised SyntaxError into a structured CodeIssue."""
    line_number = error.lineno if error.lineno and error.lineno >= 1 else 1
    column_number = max(0, (error.offset or 1) - 1)
    return CodeIssue(
        line=line_number,
        column=column_number,
        message=error.msg or str(error),
        severity=FixSeverity.ERROR,
    )


def _require_valid_syntax(
    source: str, exception_cls: type[Exception], operation: str
) -> None:
    """Validate that source parses before handing it to a lenient tool."""
    try:
        ast.parse(source)
    except SyntaxError as error:
        issue = _syntax_error_to_issue(error)
        raise exception_cls(
            f"Cannot {operation}: source has a syntax error at "
            f"line {issue.line}, column {issue.column}: {issue.message}"
        ) from error


# -----------------------------------------------------------------------------
# Syntax Auto-Fixer: Deterministic Repair Heuristics
# -----------------------------------------------------------------------------

_COMPOUND_HEADER_PATTERN: re.Pattern[str] = re.compile(
    r"^(if|elif|else|for|while|def|class|try|except|finally|with)\b"
)
_PY2_PRINT_PATTERN: re.Pattern[str] = re.compile(r"^print\s+(?!\()(.+)$")


def _is_bracket_balanced(text: str) -> bool:
    """Return True if parentheses/brackets/braces are balanced, ignoring strings."""
    depth = 0
    in_string: str | None = None
    escape = False
    for character in text:
        if in_string is not None:
            if escape:
                escape = False
            elif character == "\\":
                escape = True
            elif character == in_string:
                in_string = None
            continue
        if character in ("'", '"'):
            in_string = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
    return depth == 0


def _unclosed_bracket_stack(source: str) -> list[str]:
    """Return a stack of brackets that are opened but never closed in source."""
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = {"(", "[", "{"}
    try:
        token_generator = tokenize.generate_tokens(io.StringIO(source).readline)
        while True:
            try:
                token = next(token_generator)
            except StopIteration:
                break
            except (tokenize.TokenError, IndentationError, SyntaxError):
                break
            if token.type == tokenize.OP:
                if token.string in opens:
                    stack.append(token.string)
                elif token.string in pairs:
                    if stack and stack[-1] == pairs[token.string]:
                        stack.pop()
    except Exception as exception:
        _LOGGER.debug("Bracket-stack tokenization stopped early: %s", exception)
    return stack


def _fix_tab_indentation(source: str, error: SyntaxError) -> tuple[str, str] | None:
    """Repair mixed tab/space indentation by expanding tabs to 4 spaces."""
    is_tab_related = isinstance(error, TabError) or "tabs" in (error.msg or "").lower()
    if not is_tab_related:
        return None
    expanded = source.expandtabs(4)
    if expanded == source:
        return None
    return expanded, "Expanded tab characters to 4-space indentation"


def _fix_bracket_imbalance(source: str, _error: SyntaxError) -> tuple[str, str] | None:
    """Repair unclosed parentheses/brackets/braces by appending closers at EOF."""
    stack = _unclosed_bracket_stack(source)
    if not stack:
        return None
    closing_map = {"(": ")", "[": "]", "{": "}"}
    suffix = "".join(closing_map[bracket] for bracket in reversed(stack))
    new_source = source.rstrip("\n") + "\n" + suffix + "\n"
    return (
        new_source,
        f"Appended missing closing bracket(s) '{suffix}' to balance "
        f"{len(stack)} unclosed bracket(s)",
    )


def _fix_unterminated_string(source: str, error: SyntaxError) -> tuple[str, str] | None:
    """Repair a single-line string literal missing its closing quote."""
    message = (error.msg or "").lower()
    if (
        "unterminated string literal" not in message
        and "eol while scanning" not in message
    ):
        return None
    if error.lineno is None:
        return None
    lines = source.splitlines(keepends=True)
    index = error.lineno - 1
    if index < 0 or index >= len(lines):
        return None
    line = lines[index]
    open_quote = _find_open_quote(line)
    if open_quote is None:
        return None
    has_newline = line.endswith("\n")
    body = line[:-1] if has_newline else line
    new_line = body + open_quote + ("\n" if has_newline else "")
    lines[index] = new_line
    return "".join(lines), f"Closed unterminated string literal at line {error.lineno}"


def _find_open_quote(line_text: str) -> str | None:
    """Scan a single line and return the quote character left unclosed, if any."""
    in_string: str | None = None
    escape = False
    for character in line_text:
        if in_string is not None:
            if escape:
                escape = False
            elif character == "\\":
                escape = True
            elif character == in_string:
                in_string = None
            continue
        if character in ("'", '"'):
            in_string = character
    return in_string


def _fix_python2_print(source: str, error: SyntaxError) -> tuple[str, str] | None:
    """Convert a bare Python 2 ``print`` statement into a function call."""
    if error.lineno is None:
        return None
    lines = source.splitlines(keepends=True)
    index = error.lineno - 1
    if index < 0 or index >= len(lines):
        return None
    line = lines[index]
    stripped = line.strip()
    match = _PY2_PRINT_PATTERN.match(stripped)
    if not match:
        return None
    expression = match.group(1).rstrip()
    if ">>" in expression:
        return None
    indent = line[: len(line) - len(line.lstrip(" \t"))]
    has_newline = line.endswith("\n")
    new_line = f"{indent}print({expression})" + ("\n" if has_newline else "")
    lines[index] = new_line
    return (
        "".join(lines),
        f"Converted Python 2 'print' statement to a function call at line {error.lineno}",
    )


def _fix_missing_colon(source: str, error: SyntaxError) -> tuple[str, str] | None:
    """Append a missing trailing colon to a compound-statement header line."""
    if error.lineno is None:
        return None
    lines = source.splitlines(keepends=True)
    index = error.lineno - 1
    if index < 0 or index >= len(lines):
        return None
    line = lines[index]
    stripped = line.rstrip("\n").rstrip()
    if not stripped or stripped.endswith((":", "\\")):
        return None
    if not _COMPOUND_HEADER_PATTERN.match(stripped.lstrip()):
        return None
    if not _is_bracket_balanced(stripped):
        return None
    has_newline = line.endswith("\n")
    new_line = stripped + ":" + ("\n" if has_newline else "")
    lines[index] = new_line
    return "".join(lines), f"Inserted missing ':' at end of line {error.lineno}"


# Heuristics are tried in order; the first one that changes the source wins.
_REPAIR_HEURISTICS: list[Callable[[str, SyntaxError], tuple[str, str] | None]] = [
    _fix_tab_indentation,
    _fix_bracket_imbalance,
    _fix_unterminated_string,
    _fix_python2_print,
    _fix_missing_colon,
]


class SyntaxAutoFixer:
    """Detects and deterministically repairs common Python syntax errors."""

    @staticmethod
    def check(source: str) -> SyntaxCheckResult:
        """Validate Python source using the standard library AST parser."""
        try:
            ast.parse(source)
        except SyntaxError as error:
            return SyntaxCheckResult(
                is_valid=False, issues=[_syntax_error_to_issue(error)]
            )
        except (ValueError, RecursionError) as error:
            issue = CodeIssue(
                line=1, column=0, message=str(error), severity=FixSeverity.ERROR
            )
            return SyntaxCheckResult(is_valid=False, issues=[issue])
        return SyntaxCheckResult(is_valid=True, issues=[])

    @staticmethod
    def autofix(
        source: str, max_iterations: int = _MAX_AUTOFIX_ITERATIONS
    ) -> SyntaxFixResult:
        """Iteratively diagnose and repair syntax errors in Python source."""
        if max_iterations < 1:
            raise SyntaxFixError("max_iterations must be at least 1.")

        current_source = source
        applied_fixes: list[str] = []

        for _ in range(max_iterations):
            try:
                ast.parse(current_source)
            except SyntaxError as error:
                fix_applied = False
                for heuristic in _REPAIR_HEURISTICS:
                    outcome = heuristic(current_source, error)
                    if outcome is None:
                        continue
                    new_source, description = outcome
                    if new_source == current_source:
                        continue
                    current_source = new_source
                    applied_fixes.append(description)
                    fix_applied = True
                    break
                if not fix_applied:
                    return SyntaxFixResult(
                        success=False,
                        original_source=source,
                        fixed_source=current_source,
                        applied_fixes=applied_fixes,
                        remaining_issue=_syntax_error_to_issue(error),
                    )
            else:
                return SyntaxFixResult(
                    success=True,
                    original_source=source,
                    fixed_source=current_source,
                    applied_fixes=applied_fixes,
                    remaining_issue=None,
                )

        # Iteration budget exhausted; report the final state honestly.
        final_check = SyntaxAutoFixer.check(current_source)
        return SyntaxFixResult(
            success=final_check.is_valid,
            original_source=source,
            fixed_source=current_source,
            applied_fixes=applied_fixes,
            remaining_issue=final_check.issues[0] if final_check.issues else None,
        )


# -----------------------------------------------------------------------------
# Formatter, Import Organizer, and Unused-Code Cleanup (Black / isort / autoflake)
# -----------------------------------------------------------------------------


class CodeFormatter:
    """Wraps Black, isort, and autoflake as safe, validated, in-memory operations."""

    @staticmethod
    def format_source(
        source: str, line_length: int = DEFAULT_LINE_LENGTH
    ) -> FormatResult:
        """Format Python source using Black."""
        mode = black.Mode(line_length=line_length)
        try:
            formatted = black.format_str(source, mode=mode)
        except Exception as exception:
            raise FormattingError(
                f"Black formatting failed: {exception}"
            ) from exception
        return FormatResult(
            original_source=source,
            formatted_source=formatted,
            changed=formatted != source,
        )

    @staticmethod
    def organize_imports(
        source: str, line_length: int = DEFAULT_LINE_LENGTH
    ) -> ImportOrganizeResult:
        """Sort and group imports using isort (Black-compatible profile)."""
        _require_valid_syntax(source, ImportOrganizationError, "organize imports")
        try:
            organized = isort.code(source, profile="black", line_length=line_length)
        except Exception as exception:
            raise ImportOrganizationError(
                f"isort failed to organize imports: {exception}"
            ) from exception
        return ImportOrganizeResult(
            original_source=source,
            organized_source=organized,
            changed=organized != source,
        )

    @staticmethod
    def remove_unused(source: str) -> UnusedCleanupResult:
        """Remove unused imports and unused local variables using autoflake."""
        _require_valid_syntax(source, UnusedCodeCleanupError, "remove unused code")
        try:
            cleaned = autoflake.fix_code(
                source,
                remove_all_unused_imports=True,
                remove_unused_variables=True,
                remove_duplicate_keys=True,
                expand_star_imports=False,
            )
        except Exception as exception:
            raise UnusedCodeCleanupError(
                f"autoflake failed to remove unused code: {exception}"
            ) from exception
        return UnusedCleanupResult(
            original_source=source,
            cleaned_source=cleaned,
            changed=cleaned != source,
        )


# -----------------------------------------------------------------------------
# Code Generator: Docstrings & Unit Test Skeletons
# -----------------------------------------------------------------------------


def _format_parameter_default_repr(annotation: ast.expr | None) -> str:
    """Return a safe placeholder literal for a parameter's inferred type."""
    if annotation is None:
        return "None"
    try:
        annotation_text = ast.unparse(annotation)
    except Exception:
        return "None"
    simple_defaults: dict[str, str] = {
        "int": "0",
        "float": "0.0",
        "str": '""',
        "bool": "False",
        "bytes": 'b""',
        "list": "[]",
        "dict": "{}",
        "set": "set()",
        "tuple": "()",
    }
    return simple_defaults.get(annotation_text, "None")


def _build_docstring_lines(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, indent: str
) -> list[str]:
    """Build a Google-style docstring stub for a function/class AST node."""
    body_indent = indent + "    "
    if isinstance(node, ast.ClassDef):
        summary = f"{node.name} component."
        lines = [f'{body_indent}"""{summary}"""']
        return lines

    positional_args = [
        argument.arg
        for argument in node.args.args
        if argument.arg not in ("self", "cls")
    ]
    summary = f"{node.name.replace('_', ' ').strip().capitalize()}."
    lines = [f'{body_indent}"""{summary}']
    if positional_args:
        lines.append("")
        lines.append(f"{body_indent}Args:")
        for argument in node.args.args:
            if argument.arg in ("self", "cls"):
                continue
            annotation_text = ""
            if argument.annotation is not None:
                try:
                    annotation_text = f" ({ast.unparse(argument.annotation)})"
                except Exception:
                    annotation_text = ""
            lines.append(
                f"{body_indent}    {argument.arg}{annotation_text}: Description."
            )
    has_return_value = node.returns is not None and not (
        isinstance(node.returns, ast.Constant) and node.returns.value is None
    )
    if has_return_value:
        return_annotation = ""
        try:
            return_annotation = (
                f" ({ast.unparse(node.returns)})" if node.returns else ""
            )
        except Exception:
            return_annotation = ""
        lines.append("")
        lines.append(f"{body_indent}Returns:")
        lines.append(f"{body_indent}    Description{return_annotation}.")
    lines.append(f'{body_indent}"""')
    return lines


class CodeGenerator:
    """Generates missing docstrings and pytest unit test skeletons from an AST."""

    @staticmethod
    def generate_docstrings(source: str) -> DocstringGenerationResult:
        """Insert Google-style docstring stubs for undocumented defs/classes."""
        _require_valid_syntax(source, CodeGenerationError, "generate docstrings")
        try:
            tree = ast.parse(source)
        except SyntaxError as error:  # pragma: no cover
            raise CodeGenerationError(f"Failed to parse source: {error}") from error

        candidates: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if ast.get_docstring(node) is None and node.body:
                    # Skip definitions on single-line declarations (e.g. `def f(): pass`) to prevent syntax corruption
                    if node.lineno == node.body[0].lineno:
                        continue
                    candidates.append(node)

        # Process bottom-up so earlier insertions don't invalidate later offsets.
        candidates.sort(
            key=lambda candidate_node: candidate_node.body[0].lineno, reverse=True
        )

        lines = source.splitlines(keepends=True)
        inserted_symbols: list[str] = []
        for node in candidates:
            insertion_index = node.body[0].lineno - 1
            if insertion_index < 0 or insertion_index > len(lines):
                continue
            first_body_line = (
                lines[insertion_index] if insertion_index < len(lines) else ""
            )
            indent_match = re.match(r"^[ \t]*", first_body_line)
            indent = (
                indent_match.group(0)
                if indent_match
                else "    " * (node.col_offset // 4 + 1)
            )
            docstring_lines = _build_docstring_lines(
                node, indent[:-4] if len(indent) >= 4 else ""
            )
            newline_suffix = "\n"
            insertion_text = "".join(
                f"{docstring_line}{newline_suffix}"
                for docstring_line in docstring_lines
            )
            lines.insert(insertion_index, insertion_text)
            inserted_symbols.append(node.name)

        generated_source = "".join(lines)
        try:
            ast.parse(generated_source)
        except SyntaxError as error:
            raise CodeGenerationError(
                f"Generated docstring insertion produced invalid Python: {error}"
            ) from error

        inserted_symbols.reverse()
        return DocstringGenerationResult(
            original_source=source,
            generated_source=generated_source,
            inserted_symbols=inserted_symbols,
        )

    @staticmethod
    def generate_unit_tests(source: str, module_name: str) -> TestGenerationResult:
        """Generate a pytest test-module skeleton covering public callables."""
        _require_valid_syntax(source, CodeGenerationError, "generate unit tests")
        if not module_name.isidentifier():
            raise CodeGenerationError(
                f"'{module_name}' is not a valid Python module identifier."
            )

        tree = ast.parse(source)
        covered_symbols: list[str] = []
        test_blocks: list[str] = []

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                covered_symbols.append(node.name)
                test_blocks.append(_render_function_test(node, module_name))
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                covered_symbols.append(node.name)
                test_blocks.append(_render_class_test(node, module_name))

        header = (
            f'"""Auto-generated pytest skeleton for {module_name}.\n\n'
            "Generated by PipViper's Code Tools panel. Each test below is a "
            "scaffold: replace the placeholder arguments and assertions with "
            "real values and expectations before relying on this suite.\n"
            '"""\n\n'
            "from __future__ import annotations\n\n"
            "import pytest\n\n"
            f"from {module_name} import *  # noqa: F401,F403\n\n\n"
        )
        body = (
            "\n\n".join(test_blocks)
            if test_blocks
            else (
                "def test_module_imports() -> None:\n"
                f'    """Verify the {module_name} module imports without error."""\n'
                f"    import {module_name}  # noqa: F401\n"
            )
        )
        generated_source = header + body + "\n"

        try:
            ast.parse(generated_source)
        except SyntaxError as error:
            raise CodeGenerationError(
                f"Generated test skeleton produced invalid Python: {error}"
            ) from error

        return TestGenerationResult(
            generated_source=generated_source, covered_symbols=covered_symbols
        )


def _render_function_test(
    node: ast.FunctionDef | ast.AsyncFunctionDef, module_name: str
) -> str:
    """Render a single pytest test-function skeleton for a module-level function."""
    call_arguments = [
        _format_parameter_default_repr(argument.annotation)
        for argument in node.args.args
    ]
    call_expression = f"{module_name}.{node.name}({', '.join(call_arguments)})"
    await_prefix = "await " if isinstance(node, ast.AsyncFunctionDef) else ""
    async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    decorator = (
        "@pytest.mark.asyncio\n" if isinstance(node, ast.AsyncFunctionDef) else ""
    )
    return (
        f"{decorator}{async_prefix}def test_{node.name}() -> None:\n"
        f'    """Exercise {module_name}.{node.name} with representative input."""\n'
        f"    result = {await_prefix}{call_expression}\n"
        f"    assert result is not None  # Replace with a real expected value.\n"
    )


def _render_class_test(node: ast.ClassDef, module_name: str) -> str:
    """Render a pytest test-function skeleton exercising a class's public methods."""
    instance_variable = node.name[0].lower() + node.name[1:]

    # Context-aware extraction of constructor params
    init_node = None
    for member in node.body:
        if isinstance(member, ast.FunctionDef) and member.name == "__init__":
            init_node = member
            break

    if init_node:
        init_args = [
            _format_parameter_default_repr(argument.annotation)
            for argument in init_node.args.args
            if argument.arg not in ("self", "cls")
        ]
        construction_line = f"    {instance_variable} = {module_name}.{node.name}({', '.join(init_args)})"
    else:
        construction_line = f"    {instance_variable} = {module_name}.{node.name}()"

    lines = [
        f"def test_{node.name.lower()}_construction() -> None:",
        f'    """Verify {module_name}.{node.name} can be constructed."""',
        construction_line,
        f"    assert {instance_variable} is not None",
    ]
    for member in node.body:
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if member.name.startswith("_") or member.name == "__init__":
                continue
            call_arguments = [
                _format_parameter_default_repr(argument.annotation)
                for argument in member.args.args
                if argument.arg not in ("self", "cls")
            ]
            lines.append("")
            lines.append(f"def test_{node.name.lower()}_{member.name}() -> None:")
            lines.append(
                f'    """Exercise {module_name}.{node.name}.{member.name} '
                'with representative input."""'
            )
            lines.append(construction_line)
            lines.append(
                f"    result = {instance_variable}.{member.name}"
                f"({', '.join(call_arguments)})"
            )
            lines.append(
                "    assert result is not None  # Replace with a real expected value."
            )
    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# Symbol Renamer: Surgical Offset-Preserving Splicer
# -----------------------------------------------------------------------------


class SymbolRenamer:
    """Performs a whole-file, token-aware lexical rename of an identifier."""

    @staticmethod
    def rename(source: str, old_name: str, new_name: str) -> RenameResult:
        """Rename every free-standing occurrence of ``old_name`` to ``new_name``.

        Utilizes an offset-preserving splice engine: precomputes flat character
        offsets for lines and makes surgical textual replacements from back to
        front, safeguarding comment spacing, custom continuations, and whitespaces.
        """
        if not old_name.isidentifier() or keyword.iskeyword(old_name):
            raise RefactorError(f"'{old_name}' is not a valid identifier to rename.")
        if not new_name.isidentifier() or keyword.iskeyword(new_name):
            raise RefactorError(f"'{new_name}' is not a valid replacement identifier.")
        if old_name == new_name:
            raise RefactorError("The old and new identifier names must be different.")
        _require_valid_syntax(source, RefactorError, "rename symbol")

        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        except (tokenize.TokenError, IndentationError) as error:
            raise RefactorError(
                f"Failed to tokenize source for renaming: {error}"
            ) from error

        # Precompute flat character offsets of line boundaries to map 2D tokens to flat strings
        lines = source.splitlines(keepends=True)
        line_offsets = [0]
        for line in lines:
            line_offsets.append(line_offsets[-1] + len(line))

        spans_to_replace: list[tuple[int, int]] = []
        previous_significant_string: str | None = None

        for token in tokens:
            is_target = (
                token.type == tokenize.NAME
                and token.string == old_name
                and previous_significant_string != "."
            )
            if is_target:
                start_line, start_col = token.start
                end_line, end_col = token.end
                try:
                    start_idx = line_offsets[start_line - 1] + start_col
                    end_idx = line_offsets[end_line - 1] + end_col
                    spans_to_replace.append((start_idx, end_idx))
                except IndexError:
                    continue

            if token.type not in (
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.COMMENT,
                tokenize.ENCODING,
            ):
                previous_significant_string = token.string

        if not spans_to_replace:
            return RenameResult(
                original_source=source, renamed_source=source, occurrence_count=0
            )

        # Sort spans in descending order to modify from back-to-front without index shifts
        spans_to_replace.sort(key=lambda span_item: span_item[0], reverse=True)

        current_source = source
        for start_idx, end_idx in spans_to_replace:
            current_source = (
                current_source[:start_idx] + new_name + current_source[end_idx:]
            )

        try:
            ast.parse(current_source)
        except SyntaxError as error:
            raise RefactorError(
                f"Rename produced invalid Python and was discarded: {error}"
            ) from error

        return RenameResult(
            original_source=source,
            renamed_source=current_source,
            occurrence_count=len(spans_to_replace),
        )


# -----------------------------------------------------------------------------
# Full Pipeline
# -----------------------------------------------------------------------------


class CodeToolsPipeline:
    """Chains the auto-fixer, cleanup, import organizer, and formatter."""

    @staticmethod
    def run_full_pipeline(
        source: str, line_length: int = DEFAULT_LINE_LENGTH
    ) -> PipelineResult:
        """Run auto-fix, unused-code cleanup, import organization, and formatting."""
        steps: list[PipelineStepResult] = []
        current_source = source

        check_result = SyntaxAutoFixer.check(current_source)
        if not check_result.is_valid:
            fix_result = SyntaxAutoFixer.autofix(current_source)
            if not fix_result.success:
                issue = fix_result.remaining_issue
                location = (
                    f"line {issue.line}, column {issue.column}"
                    if issue
                    else "unknown location"
                )
                message = issue.message if issue else "unknown syntax error"
                raise SyntaxFixError(
                    "The pipeline could not proceed: the source has a syntax "
                    f"error at {location} ({message}) that the auto-fixer "
                    "could not automatically resolve. Fix it manually and "
                    "re-run the pipeline."
                )
            steps.append(
                PipelineStepResult(
                    step_name="Auto-Fix Syntax",
                    changed=True,
                    detail="; ".join(fix_result.applied_fixes)
                    or "Repaired syntax errors.",
                )
            )
            current_source = fix_result.fixed_source
        else:
            steps.append(
                PipelineStepResult(
                    step_name="Auto-Fix Syntax",
                    changed=False,
                    detail="No syntax errors found.",
                )
            )

        cleanup_result = CodeFormatter.remove_unused(current_source)
        steps.append(
            PipelineStepResult(
                step_name="Remove Unused Imports/Variables",
                changed=cleanup_result.changed,
                detail=(
                    "Removed unused imports/variables."
                    if cleanup_result.changed
                    else "No unused imports or variables found."
                ),
            )
        )
        current_source = cleanup_result.cleaned_source

        organize_result = CodeFormatter.organize_imports(
            current_source, line_length=line_length
        )
        steps.append(
            PipelineStepResult(
                step_name="Organize Imports",
                changed=organize_result.changed,
                detail=(
                    "Reorganized import statements."
                    if organize_result.changed
                    else "Imports already organized."
                ),
            )
        )
        current_source = organize_result.organized_source

        format_result = CodeFormatter.format_source(
            current_source, line_length=line_length
        )
        steps.append(
            PipelineStepResult(
                step_name="Format (Black)",
                changed=format_result.changed,
                detail=(
                    "Reformatted source."
                    if format_result.changed
                    else "Already formatted."
                ),
            )
        )
        current_source = format_result.formatted_source

        return PipelineResult(
            original_source=source, final_source=current_source, steps=steps
        )
