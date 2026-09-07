"""Code Tools & Refactoring Service for PipViper IDE.

Encapsulates formatting, syntax auto-fixing, import organization, and symbol renaming.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..code_tools import (
    CodeFormatter,
    CodeGenerator,
    CodeToolsPipeline,
    FormatResult,
    ImportOrganizeResult,
    PipelineResult,
    RenameResult,
    SymbolRenamer,
    SyntaxAutoFixer,
    SyntaxCheckResult,
    SyntaxFixResult,
    UnusedCleanupResult,
)

_LOGGER = logging.getLogger("src.services.code_tools")


class CodeToolsService(QObject):
    """Manages local, offline code transformations and refactoring tools."""

    _instance: Optional[CodeToolsService] = None

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.formatter = CodeFormatter()
        self.fixer = SyntaxAutoFixer()
        self.renamer = SymbolRenamer()
        self.generator = CodeGenerator()
        self.pipeline = CodeToolsPipeline()

    @classmethod
    def get_instance(cls) -> CodeToolsService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def format_code(self, source: str) -> FormatResult:
        return self.formatter.format_source(source)

    def check_syntax(self, source: str) -> SyntaxCheckResult:
        return self.fixer.check(source)

    def auto_fix_syntax(self, source: str) -> SyntaxFixResult:
        return self.fixer.autofix(source)

    def organize_imports(self, source: str) -> ImportOrganizeResult:
        return self.formatter.organize_imports(source)

    def cleanup_unused(self, source: str) -> UnusedCleanupResult:
        return self.formatter.cleanup_unused(source)

    def rename_symbol(self, source: str, old_name: str, new_name: str) -> RenameResult:
        return self.renamer.rename_symbol(source, old_name, new_name)

    def run_clean_pipeline(self, source: str) -> PipelineResult:
        return self.pipeline.clean_code(source)
