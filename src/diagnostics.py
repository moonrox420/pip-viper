"""Real-time hybrid diagnostic engine for PipViper.

Provides sub-10ms in-memory linting and code diagnostics using Astral's Ruff
via stdin pipes, with fallback to Python's built-in AST parser. Also supports
on-demand static type analysis via Mypy.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

_LOGGER = logging.getLogger("src.diagnostics")


# -----------------------------------------------------------------------------
# Diagnostic Models
# -----------------------------------------------------------------------------


class EditLocation(BaseModel):
    """Row and column coordinates of a text edit location (1-indexed)."""

    row: int
    column: int


class FixEdit(BaseModel):
    """Specific replacement text and range for a diagnostic auto-fix."""

    content: str
    location: EditLocation
    end_location: EditLocation


class DiagnosticFix(BaseModel):
    """Auto-fix metadata containing edit instructions from Ruff."""

    message: str
    applicability: str = "safe"  # "safe" | "unsafe"
    edits: list[FixEdit] = Field(default_factory=list)


class DiagnosticIssue(BaseModel):
    """Structured diagnostic issue representing a lint warning or error."""

    code: str
    message: str
    severity: str = "error"  # "error" | "warning" | "info"
    file_path: str = ""
    line: int
    column: int
    end_line: int
    end_column: int
    fix: Optional[DiagnosticFix] = None
    url: Optional[str] = None

    @property
    def has_fix(self) -> bool:
        """Return True if this issue has an applicable auto-fix."""
        return self.fix is not None and len(self.fix.edits) > 0

    @property
    def fix_message(self) -> Optional[str]:
        """Return human-readable description of the auto-fix if available."""
        return self.fix.message if self.fix is not None else None


# -----------------------------------------------------------------------------
# Ruff Diagnostic Service
# -----------------------------------------------------------------------------


class RuffService:
    """High-performance in-memory diagnostic service powered by Ruff."""

    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = project_root
        self._executable_cache: str | None = None

    def find_executable(self) -> str | None:
        """Locate the ruff executable in the virtual environment or PATH."""
        if self._executable_cache is not None:
            return self._executable_cache

        candidates: list[Path] = []
        if self._project_root:
            if sys.platform == "win32":
                candidates.append(self._project_root / ".venv" / "Scripts" / "ruff.exe")
                candidates.append(self._project_root / "venv" / "Scripts" / "ruff.exe")
            else:
                candidates.append(self._project_root / ".venv" / "bin" / "ruff")
                candidates.append(self._project_root / "venv" / "bin" / "ruff")

        # Check Python directory if running within a venv
        sys_prefix = Path(sys.prefix)
        if sys.platform == "win32":
            candidates.append(sys_prefix / "Scripts" / "ruff.exe")
        else:
            candidates.append(sys_prefix / "bin" / "ruff")

        for candidate in candidates:
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                self._executable_cache = str(candidate)
                return self._executable_cache

        # Fallback to system PATH
        system_ruff = shutil.which("ruff")
        if system_ruff:
            self._executable_cache = system_ruff
            return system_ruff

        return None

    def check_source(
        self, source: str, file_path: Path | str = "untitled.py"
    ) -> list[DiagnosticIssue]:
        """Run Ruff diagnostics on Python source code via stdin.

        If Ruff is unavailable or fails, falls back to Python's built-in AST
        syntax validation.
        """
        executable = self.find_executable()
        if not executable:
            return self._fallback_ast_check(source, file_path)

        cmd = [
            executable,
            "check",
            "--output-format=json",
            "--stdin-filename",
            str(file_path),
            "-",
        ]

        try:
            cwd = str(self._project_root) if self._project_root else None
            extra_kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                extra_kwargs["startupinfo"] = startupinfo

            result = subprocess.run(
                cmd,
                input=source.encode("utf-8"),
                capture_output=True,
                cwd=cwd,
                timeout=5,
                **extra_kwargs,
            )
            stdout = result.stdout.decode("utf-8", errors="replace").strip()
            if not stdout:
                return []
            return self._parse_ruff_json(stdout, str(file_path))
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as exc:
            _LOGGER.warning("Ruff check invocation failed: %s", exc)
            return self._fallback_ast_check(source, file_path)

    def _parse_ruff_json(
        self, raw_json: str, default_path: str
    ) -> list[DiagnosticIssue]:
        """Parse Ruff's JSON diagnostic output into DiagnosticIssue instances."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            _LOGGER.debug("Failed to parse Ruff JSON output: %s", exc)
            return []

        if not isinstance(data, list):
            return []

        issues: list[DiagnosticIssue] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "lint")
            message = item.get("message", "")
            raw_severity = item.get("severity", "error")
            severity = "error" if raw_severity in ("error", "fatal") else "warning"

            loc = item.get("location", {})
            end_loc = item.get("end_location", {})

            line = int(loc.get("row", 1))
            col = int(loc.get("column", 1))
            end_line = int(end_loc.get("row", line))
            end_col = int(end_loc.get("column", col + 1))

            fix_obj: Optional[DiagnosticFix] = None
            raw_fix = item.get("fix")
            if isinstance(raw_fix, dict):
                edits_list: list[FixEdit] = []
                for edit in raw_fix.get("edits", []):
                    if not isinstance(edit, dict):
                        continue
                    e_loc = edit.get("location", {})
                    e_end = edit.get("end_location", {})
                    edits_list.append(
                        FixEdit(
                            content=edit.get("content", ""),
                            location=EditLocation(
                                row=int(e_loc.get("row", 1)),
                                column=int(e_loc.get("column", 1)),
                            ),
                            end_location=EditLocation(
                                row=int(e_end.get("row", 1)),
                                column=int(e_end.get("column", 1)),
                            ),
                        )
                    )
                fix_obj = DiagnosticFix(
                    message=raw_fix.get("message", "Quick fix"),
                    applicability=raw_fix.get("applicability", "safe"),
                    edits=edits_list,
                )

            issue = DiagnosticIssue(
                code=code,
                message=message,
                severity=severity,
                file_path=item.get("filename", default_path),
                line=line,
                column=col,
                end_line=end_line,
                end_column=end_col,
                fix=fix_obj,
                url=item.get("url"),
            )
            issues.append(issue)

        return issues

    def _fallback_ast_check(
        self, source: str, file_path: Path | str
    ) -> list[DiagnosticIssue]:
        """Perform fallback syntax validation using Python's built-in ast module."""
        try:
            ast.parse(source, filename=str(file_path))
            return []
        except SyntaxError as syn_err:
            line = syn_err.lineno or 1
            col = syn_err.offset or 1
            end_line = syn_err.end_lineno or line
            end_col = syn_err.end_offset or col + 1
            return [
                DiagnosticIssue(
                    code="E999",
                    message=syn_err.msg or "SyntaxError",
                    severity="error",
                    file_path=str(file_path),
                    line=line,
                    column=col,
                    end_line=end_line,
                    end_column=end_col,
                )
            ]

    @staticmethod
    def apply_fix(source: str, issue: DiagnosticIssue) -> str:
        """Apply the safe diagnostic fix edits from an issue to source code.

        Edits are applied in reverse document order to preserve accurate
        character coordinate offsets.
        """
        if not issue.fix or not issue.fix.edits:
            return source

        lines = source.splitlines(keepends=True)
        if not lines:
            return source

        # Precompute character offset for the start of each line (0-indexed line array)
        line_offsets = [0]
        for line_str in lines:
            line_offsets.append(line_offsets[-1] + len(line_str))

        def to_char_index(loc: EditLocation) -> int:
            row_idx = max(0, min(loc.row - 1, len(lines) - 1))
            line_start = line_offsets[row_idx]
            line_len = len(lines[row_idx])
            col_offset = max(0, min(loc.column - 1, line_len))
            return line_start + col_offset

        # Sort edits in reverse order by start character position
        sorted_edits = sorted(
            issue.fix.edits,
            key=lambda e: to_char_index(e.location),
            reverse=True,
        )

        modified = source
        for edit in sorted_edits:
            start_pos = to_char_index(edit.location)
            end_pos = to_char_index(edit.end_location)
            if start_pos <= end_pos and end_pos <= len(modified):
                modified = modified[:start_pos] + edit.content + modified[end_pos:]

        return modified


# -----------------------------------------------------------------------------
# Mypy Static Type Checking Service
# -----------------------------------------------------------------------------


class MypyService:
    """Asynchronous on-demand static type checking runner."""

    _MYPY_LINE_PATTERN: re.Pattern[str] = re.compile(
        r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+(?P<sev>error|warning|note):\s+(?P<msg>.+?)(?:\s+\[(?P<code>.+?)\])?$"
    )

    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = project_root
        self._executable_cache: str | None = None

    def find_executable(self) -> str | None:
        """Locate the mypy executable in the virtual environment or PATH."""
        if self._executable_cache is not None:
            return self._executable_cache

        candidates: list[Path] = []
        if self._project_root:
            if sys.platform == "win32":
                candidates.append(self._project_root / ".venv" / "Scripts" / "mypy.exe")
            else:
                candidates.append(self._project_root / ".venv" / "bin" / "mypy")

        sys_prefix = Path(sys.prefix)
        if sys.platform == "win32":
            candidates.append(sys_prefix / "Scripts" / "mypy.exe")
        else:
            candidates.append(sys_prefix / "bin" / "mypy")

        for candidate in candidates:
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                self._executable_cache = str(candidate)
                return self._executable_cache

        system_mypy = shutil.which("mypy")
        if system_mypy:
            self._executable_cache = system_mypy
            return system_mypy

        return None

    def check_file(self, file_path: Path) -> list[DiagnosticIssue]:
        """Execute Mypy type-checking against a target file and return issues."""
        executable = self.find_executable()
        if not executable or not file_path.exists():
            return []

        cmd = [
            executable,
            "--show-column-numbers",
            "--show-error-codes",
            "--no-error-summary",
            "--ignore-missing-imports",
            str(file_path),
        ]

        try:
            cwd = str(self._project_root) if self._project_root else str(file_path.parent)
            extra_kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                extra_kwargs["startupinfo"] = startupinfo

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=15,
                **extra_kwargs,
            )
            return self._parse_mypy_output(proc.stdout, file_path)
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as exc:
            _LOGGER.warning("Mypy check invocation failed: %s", exc)
            return []

    def _parse_mypy_output(
        self, output: str, target_file: Path
    ) -> list[DiagnosticIssue]:
        """Parse line-based Mypy diagnostics into DiagnosticIssue items."""
        issues: list[DiagnosticIssue] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            match = self._MYPY_LINE_PATTERN.match(line)
            if not match:
                continue

            file_str = match.group("file")
            line_num = int(match.group("line"))
            col_num = int(match.group("col"))
            raw_sev = match.group("sev").lower()
            if raw_sev == "note":
                continue
            severity = "error" if raw_sev == "error" else "warning"
            msg = match.group("msg").strip()
            code = match.group("code") or "type-check"

            # Filter only issues for target file if path can be resolved
            try:
                issue_path = Path(file_str)
                if issue_path.name != target_file.name:
                    continue
            except Exception:
                pass

            issues.append(
                DiagnosticIssue(
                    code=code,
                    message=msg,
                    severity=severity,
                    file_path=str(target_file),
                    line=line_num,
                    column=col_num,
                    end_line=line_num,
                    end_column=col_num + 1,
                )
            )

        return issues
