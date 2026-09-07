"""Code Tools Controller for PipViper IDE.

Orchestrates code intelligence actions: syntax check, autofix, formatting,
import organization, dead code removal, docstring and test generation,
and symbol renaming.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QDialog, QWidget

from ..code_tools import (
    CodeGenerator,
    DocstringGenerationResult,
    FormatResult,
    ImportOrganizeResult,
    PipelineResult,
    RenameResult,
    SyntaxCheckResult,
    SyntaxFixResult,
    TestGenerationResult,
    UnusedCleanupResult,
)
from ..diff_viewer import DiffDialog
from ..editor import CodeEditor, EditorTabs
from ..panels.code_tools_panel import CodeToolsPanel
from ..pip_viper import ColorPalette, run_in_thread
from ..services.code_tools_service import CodeToolsService

_LOGGER = logging.getLogger("src.controllers.code_tools")


class CodeToolsController(QObject):
    """Controller orchestrating code manipulation and refactoring operations."""

    def __init__(
        self,
        editor_tabs: EditorTabs,
        panel: CodeToolsPanel,
        service: Optional[CodeToolsService] = None,
        palette: Optional[ColorPalette] = None,
        on_applied_callback: Optional[Callable[[CodeEditor], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._tabs = editor_tabs
        self._panel = panel
        self._service = service or CodeToolsService.get_instance()
        self._palette = palette
        self._on_applied = on_applied_callback
        self._parent_widget = parent

        self._wire_panel_signals()

    def set_palette(self, palette: ColorPalette) -> None:
        self._palette = palette

    def _wire_panel_signals(self) -> None:
        self._panel.check_syntax_requested.connect(self.check_syntax)
        self._panel.autofix_requested.connect(self.autofix)
        self._panel.format_requested.connect(self.format_code)
        self._panel.organize_imports_requested.connect(self.organize_imports)
        self._panel.remove_unused_requested.connect(self.remove_unused)
        self._panel.full_pipeline_requested.connect(self.run_full_pipeline)
        self._panel.generate_docstrings_requested.connect(self.generate_docstrings)
        self._panel.generate_tests_requested.connect(self.generate_tests)
        self._panel.rename_requested.connect(self.rename_symbol)

    def preview_and_apply(
        self,
        editor: CodeEditor,
        original_source: str,
        new_source: str,
        operation_label: str,
    ) -> bool:
        """Show side-by-side diff review for a code transformation and apply if accepted."""
        if new_source == original_source:
            self._panel.log(f"{operation_label}: no changes needed.")
            return False

        if not self._palette:
            editor.replace_content(new_source)
            self._panel.log(f"{operation_label}: changes applied directly.")
            if self._on_applied:
                self._on_applied(editor)
            return True

        dialog = DiffDialog(original_source, new_source, self._palette, self._parent_widget)
        dialog.setWindowTitle(f"Review: {operation_label}")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            editor.replace_content(dialog.get_modified_code())
            self._panel.log(f"{operation_label}: changes applied.")
            if self._on_applied:
                self._on_applied(editor)
            return True
        else:
            self._panel.log(f"{operation_label}: changes discarded.")
            return False

    @Slot()
    def check_syntax(self) -> None:
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._panel.set_busy(True)

        def perform() -> SyntaxCheckResult:
            return self._service.check_syntax(source)

        def on_success(result: SyntaxCheckResult) -> None:
            self._panel.set_busy(False)
            self._panel.populate_issues(result.issues)
            editor.set_syntax_issues(result.issues)
            if result.is_valid:
                self._panel.log("✓ No syntax errors found.")
            else:
                self._panel.log(f"✗ {len(result.issues)} syntax issue(s) found.")

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Syntax check failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def autofix(self) -> None:
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._panel.set_busy(True)

        def perform() -> SyntaxFixResult:
            return self._service.auto_fix_syntax(source)

        def on_success(result: SyntaxFixResult) -> None:
            self._panel.set_busy(False)
            if not result.applied_fixes:
                self._panel.log("Auto-fix: no automated fixes were applicable.")
                return
            applied_desc = ", ".join(result.applied_fixes)
            self.preview_and_apply(
                editor,
                source,
                result.fixed_source,
                f"Auto-fix ({applied_desc})",
            )
            self.check_syntax()

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Auto-fix failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def format_code(self) -> None:
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._panel.set_busy(True)

        def perform() -> FormatResult:
            return self._service.format_code(source)

        def on_success(result: FormatResult) -> None:
            self._panel.set_busy(False)
            if not result.success:
                self._panel.log(f"Formatting failed: {result.error_message}")
                return
            if not result.changed:
                self._panel.log("Code is already formatted.")
                return
            self.preview_and_apply(
                editor,
                source,
                result.formatted_source,
                f"Format Code ({result.formatter_used})",
            )

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Formatting error: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def organize_imports(self) -> None:
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._panel.set_busy(True)

        def perform() -> ImportOrganizeResult:
            return self._service.organize_imports(source)

        def on_success(result: ImportOrganizeResult) -> None:
            self._panel.set_busy(False)
            if not result.changed:
                self._panel.log("Imports are already organized.")
                return
            self.preview_and_apply(
                editor,
                source,
                result.organized_source,
                "Organize Imports",
            )

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Organize imports failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def remove_unused(self) -> None:
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._panel.set_busy(True)

        def perform() -> UnusedCleanupResult:
            return self._service.cleanup_unused(source)

        def on_success(result: UnusedCleanupResult) -> None:
            self._panel.set_busy(False)
            if not result.removed_names:
                self._panel.log("Remove unused: no unused imports found.")
                return
            removed = ", ".join(result.removed_names)
            self.preview_and_apply(
                editor,
                source,
                result.cleaned_source,
                f"Remove Unused ({removed})",
            )

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Cleanup unused failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def run_full_pipeline(self) -> None:
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._panel.set_busy(True)

        def perform() -> PipelineResult:
            return self._service.run_clean_pipeline(source)

        def on_success(result: PipelineResult) -> None:
            self._panel.set_busy(False)
            steps_desc = " -> ".join(result.steps_applied) if result.steps_applied else "None"
            self._panel.log(f"Pipeline finished. Steps: {steps_desc}")
            if result.final_source != source:
                self.preview_and_apply(
                    editor,
                    source,
                    result.final_source,
                    f"Full Pipeline ({steps_desc})",
                )
            else:
                self._panel.log("Pipeline: code is already clean.")

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Pipeline failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def generate_docstrings(self) -> None:
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._panel.set_busy(True)

        def perform() -> DocstringGenerationResult:
            return CodeGenerator.generate_docstrings(source)

        def on_success(result: DocstringGenerationResult) -> None:
            self._panel.set_busy(False)
            if result.inserted_symbols:
                self._panel.log(
                    "Generated docstrings for: " + ", ".join(result.inserted_symbols)
                )
            else:
                self._panel.log("Generate Docstrings: nothing to document.")
            self.preview_and_apply(
                editor, source, result.generated_source, "Generate Docstrings"
            )

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Docstring generation failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def generate_tests(self) -> None:
        import re
        from pathlib import Path
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        raw_stem = editor.file_path().stem or "module"
        module_name = (
            raw_stem if raw_stem.isidentifier() else re.sub(r"\W|^(?=\d)", "_", raw_stem)
        )
        self._panel.set_busy(True)

        def perform() -> TestGenerationResult:
            return CodeGenerator.generate_unit_tests(source, module_name)

        def on_success(result: TestGenerationResult) -> None:
            self._panel.set_busy(False)
            if result.covered_symbols:
                self._panel.log(
                    "Generated tests covering: " + ", ".join(result.covered_symbols)
                )
            else:
                self._panel.log(
                    "Generate Unit Tests: no public symbols found; "
                    "generated an import-only skeleton."
                )
            test_path = Path(f"test_{module_name}.py")
            existing_test_editor = self._tabs.editor_for_path(test_path)
            if existing_test_editor is not None:
                self._tabs.set_current_editor(existing_test_editor)
                self.preview_and_apply(
                    existing_test_editor,
                    existing_test_editor.toPlainText(),
                    result.generated_source,
                    "Regenerate Unit Tests",
                )
                return
            new_test_editor = self._tabs.add_editor(
                test_path, result.generated_source
            )
            new_test_editor.document().setModified(True)

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Test generation failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot(str, str)
    def rename_symbol(self, old_name: str, new_name: str) -> None:
        editor = self._tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._panel.set_busy(True)

        def perform() -> RenameResult:
            from ..code_tools import SymbolRenamer
            return SymbolRenamer.rename(source, old_name, new_name)

        def on_success(result: RenameResult) -> None:
            self._panel.set_busy(False)
            if result.occurrence_count == 0:
                self._panel.log(f"Rename: no occurrences of '{old_name}' found.")
                return
            self._panel.log(
                f"Rename: {result.occurrence_count} occurrence(s) of "
                f"'{old_name}' -> '{new_name}'."
            )
            self.preview_and_apply(
                editor,
                source,
                result.renamed_source,
                f"Rename '{old_name}' -> '{new_name}'",
            )

        def on_error(message: str) -> None:
            self._panel.set_busy(False)
            self._panel.log(f"Rename failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

