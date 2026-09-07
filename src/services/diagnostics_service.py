"""Workspace Diagnostics Service for PipViper IDE.

Encapsulates multi-file diagnostics, AST parsing, and background linters.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..diagnostics import (
    DiagnosticIssue,
    MypyService,
    RuffService,
)
from .process_service import ProcessService

_LOGGER = logging.getLogger("src.services.diagnostics")


class DiagnosticsService(QObject):
    """Manages workspace-level background diagnostics and problem reporting."""

    diagnostics_updated = Signal(list)  # Emits list[DiagnosticIssue]

    _instance: Optional[DiagnosticsService] = None

    def __init__(
        self,
        project_root: Optional[Path] = None,
        process_service: Optional[ProcessService] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._project_root = project_root
        self._process = process_service or ProcessService.get_instance()
        self._ruff = RuffService(project_root)
        self._mypy = MypyService(project_root)

    @classmethod
    def get_instance(cls) -> DiagnosticsService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def scan_source(self, source_code: str, file_path: Path | str = "untitled.py") -> list[DiagnosticIssue]:
        issues = self._ruff.check_source(source_code, file_path)
        self.diagnostics_updated.emit(issues)
        return issues

    def run_mypy(self, target_path: Path) -> list[DiagnosticIssue]:
        issues = self._mypy.check_file(target_path)
        self.diagnostics_updated.emit(issues)
        return issues
