"""Linter and Static Analysis Service for PipViper IDE.

Encapsulates executing Flake8, Pylint, Mypy, and Ruff using ProcessService (PRD A3).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, Signal

from ..pip_viper import LintIssue, LintSeverity, LintTool
from .process_service import ProcessService

_LOGGER = logging.getLogger("src.services.linter")

_LINTER_OUTPUT_PATTERNS: dict[LintTool, re.Pattern[str]] = {
    LintTool.FLAKE8: re.compile(
        r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>[A-Z]\d+)\s+(?P<msg>.+)$"
    ),
    LintTool.PYLINT: re.compile(
        r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>[CWEF]\d+):\s*(?P<msg>.+)$"
    ),
    LintTool.MYPY: re.compile(
        r"^(?P<file>[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<msg>.+)$"
    ),
}

_LINTER_SEVERITY_MAPPERS: dict[LintTool, Callable[[str], LintSeverity]] = {
    LintTool.FLAKE8: lambda code: LintSeverity.WARNING,
    LintTool.PYLINT: lambda code: (
        LintSeverity.ERROR if code.startswith("E") else LintSeverity.WARNING
    ),
    LintTool.MYPY: lambda _code: LintSeverity.ERROR,
}


class LinterService(QObject):
    """Manages execution of Python static analysis tools."""

    lint_completed = Signal(list)  # Emits list[LintIssue]

    _instance: Optional[LinterService] = None

    def __init__(
        self,
        process_service: Optional[ProcessService] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._process = process_service or ProcessService.get_instance()

    @classmethod
    def get_instance(cls) -> LinterService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def run_linter(
        self,
        tool: LintTool,
        file_path: Path,
        python_bin: str,
        cwd: Optional[Path] = None,
        timeout: float = 30.0,
    ) -> list[LintIssue]:
        """Execute the specified linter on a source file."""
        if not file_path.is_file():
            return []

        module_name = tool.value
        cmd = [python_bin, "-m", module_name, str(file_path)]
        try:
            res = self._process.run_command(cmd, cwd=cwd, timeout=timeout)
            raw_output = res.stdout + "\n" + res.stderr
            issues = self.parse_output(tool, raw_output, file_path)
            self.lint_completed.emit(issues)
            return issues
        except Exception as exc:
            _LOGGER.warning("Linter %s execution failed: %s", tool.value, exc)
            return []

    def parse_output(self, tool: LintTool, output: str, target_file: Path) -> list[LintIssue]:
        """Parse raw linter output lines into structured LintIssue models."""
        pattern = _LINTER_OUTPUT_PATTERNS.get(tool)
        severity_mapper = _LINTER_SEVERITY_MAPPERS.get(tool, lambda _: LintSeverity.WARNING)
        if not pattern:
            return []

        issues: list[LintIssue] = []
        for line in output.splitlines():
            line_str = line.strip()
            match = pattern.match(line_str)
            if match:
                groups = match.groupdict()
                code_val = groups.get("code") or ""
                col_val = int(groups.get("col") or 1)
                line_val = int(groups.get("line") or 1)
                msg_val = groups.get("msg") or ""
                issues.append(
                    LintIssue(
                        file_path=target_file,
                        line=line_val,
                        column=col_val,
                        code=code_val,
                        message=msg_val,
                        severity=severity_mapper(code_val),
                        tool=tool,
                    )
                )
        return issues
